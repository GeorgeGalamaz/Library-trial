import sqlite3
from difflib import SequenceMatcher
from pathlib import Path

import cloudinary
import cloudinary.uploader
import pandas as pd
import requests
import streamlit as st
from PIL import Image
from pyzbar.pyzbar import decode as decode_barcodes

DB_PATH = Path(__file__).parent / "library.db"
MATCH_THRESHOLD = 0.6  # below this, ask the user instead of guessing

st.set_page_config(page_title="My Library", page_icon="📚", layout="wide")


# ---------- Cloudinary ----------

def configure_cloudinary():
    cloudinary.config(
        cloud_name=st.secrets["cloudinary"]["cloud_name"],
        api_key=st.secrets["cloudinary"]["api_key"],
        api_secret=st.secrets["cloudinary"]["api_secret"],
    )


def upload_photo(file) -> str:
    configure_cloudinary()
    result = cloudinary.uploader.upload(file)
    return result["secure_url"]


# ---------- Database helpers ----------

def get_connection():
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def init_db():
    conn = get_connection()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS books (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            author TEXT NOT NULL,
            title TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            book_id INTEGER NOT NULL,
            photo_type TEXT NOT NULL,
            url TEXT NOT NULL,
            FOREIGN KEY (book_id) REFERENCES books (id)
        )
        """
    )
    conn.commit()
    conn.close()


def get_all_books() -> pd.DataFrame:
    conn = get_connection()
    df = pd.read_sql_query("SELECT id, author, title FROM books ORDER BY author, title", conn)
    conn.close()
    return df


def get_photos(book_id: int) -> pd.DataFrame:
    conn = get_connection()
    df = pd.read_sql_query(
        "SELECT id, photo_type, url FROM photos WHERE book_id = ? ORDER BY photo_type, id",
        conn, params=(book_id,),
    )
    conn.close()
    return df


def add_photo(book_id: int, photo_type: str, url: str):
    conn = get_connection()
    conn.execute(
        "INSERT INTO photos (book_id, photo_type, url) VALUES (?, ?, ?)",
        (book_id, photo_type, url),
    )
    conn.commit()
    conn.close()


def delete_photo(photo_id: int):
    conn = get_connection()
    conn.execute("DELETE FROM photos WHERE id = ?", (photo_id,))
    conn.commit()
    conn.close()


def add_book(author: str, title: str):
    conn = get_connection()
    conn.execute("INSERT INTO books (author, title) VALUES (?, ?)", (author.strip(), title.strip()))
    conn.commit()
    conn.close()


def delete_book(book_id: int):
    conn = get_connection()
    conn.execute("DELETE FROM books WHERE id = ?", (book_id,))
    conn.execute("DELETE FROM photos WHERE book_id = ?", (book_id,))
    conn.commit()
    conn.close()


def reset_database():
    conn = get_connection()
    conn.execute("DELETE FROM books")
    conn.execute("DELETE FROM photos")
    conn.commit()
    conn.close()


def import_excel(file, has_header: bool):
    """Reads an Excel file with two columns (author, title) and inserts new rows into the DB.
    Rows matching an existing (author, title) pair are skipped to avoid duplicates."""
    header_arg = 0 if has_header else None
    df = pd.read_excel(file, header=header_arg)
    df = df.iloc[:, :2]
    df.columns = ["author", "title"]
    df = df.dropna(how="all")
    df["author"] = df["author"].astype(str).str.strip()
    df["title"] = df["title"].astype(str).str.strip()

    existing = get_all_books()
    existing_pairs = set(zip(existing["author"], existing["title"]))
    df = df[~df.apply(lambda r: (r["author"], r["title"]) in existing_pairs, axis=1)]

    conn = get_connection()
    if not df.empty:
        df.to_sql("books", conn, if_exists="append", index=False)
        conn.commit()
    conn.close()
    return len(df)


def book_count() -> int:
    conn = get_connection()
    count = conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
    conn.close()
    return count


def load_seed_if_empty():
    """On a fresh/reset database, auto-import books_seed.csv if it exists next to app.py."""
    seed_path = Path(__file__).parent / "books_seed.csv"
    if book_count() == 0 and seed_path.exists():
        df = pd.read_csv(seed_path)
        conn = get_connection()
        df.to_sql("books", conn, if_exists="append", index=False)
        conn.commit()
        conn.close()


# ---------- Filename matching ----------

def parse_photo_filename(filename: str):
    """'Author - Title.jpg' -> cover. 'Author - Title - toc2.jpg' -> toc page.
    Returns (author_guess, title_guess, photo_type)."""
    stem = Path(filename).stem
    parts = [p.strip() for p in stem.split(" - ")]
    if len(parts) >= 2:
        author_guess, title_guess = parts[0], parts[1]
        tail = parts[2].lower() if len(parts) > 2 else ""
    else:
        author_guess, title_guess, tail = "", stem, ""
    photo_type = "toc" if tail.startswith("toc") or tail.startswith("cuprins") else "cover"
    return author_guess, title_guess, photo_type


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def find_best_match(author_guess: str, title_guess: str, books_df: pd.DataFrame):
    """Returns (book_id, score) for the closest book, or (None, 0) if the library is empty."""
    if books_df.empty:
        return None, 0.0
    guess = f"{author_guess} {title_guess}"
    scores = books_df.apply(lambda r: similarity(guess, f"{r['author']} {r['title']}"), axis=1)
    best_idx = scores.idxmax()
    return books_df.loc[best_idx, "id"], scores.loc[best_idx]


# ---------- Barcode scanning ----------

def extract_isbn(photo) -> str | None:
    """Decodes barcodes from a photo and returns the first one that looks like a book ISBN
    (13-digit EAN starting with 978 or 979 — the 'Bookland' prefix used for all books)."""
    image = Image.open(photo)
    codes = [b.data.decode("utf-8") for b in decode_barcodes(image)]
    for code in codes:
        if len(code) == 13 and (code.startswith("978") or code.startswith("979")):
            return code
    return codes[0] if codes else None


def lookup_isbn(isbn: str):
    """Looks up an ISBN on Open Library. Returns a dict with title/author/cover_url, or None."""
    try:
        resp = requests.get(
            "https://openlibrary.org/api/books",
            params={"bibkeys": f"ISBN:{isbn}", "format": "json", "jscmd": "data"},
            timeout=6,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException:
        return None

    key = f"ISBN:{isbn}"
    if key not in data:
        return None

    book = data[key]
    title = book.get("title", "")
    author = ", ".join(a.get("name", "") for a in book.get("authors", []))
    cover_url = (book.get("cover") or {}).get("large") or (book.get("cover") or {}).get("medium")
    if not title:
        return None
    return {"title": title, "author": author, "cover_url": cover_url}


# ---------- App ----------

init_db()
load_seed_if_empty()

st.title("📚 My Library")

with st.sidebar:
    st.header("Import from Excel")
    st.caption("Two columns: author, title. Works with or without a header row.")
    uploaded_file = st.file_uploader("Choose an Excel file", type=["xlsx", "xls"])
    has_header = st.checkbox("My file has a header row", value=False)

    if uploaded_file is not None:
        if st.button("Import into library", type="primary"):
            n = import_excel(uploaded_file, has_header)
            st.success(f"Imported {n} books.")
            st.rerun()

    st.divider()
    st.header("⚠️ Reset database")
    st.caption("Deletes every book and photo. Use this if an import got duplicated.")
    confirm_reset = st.checkbox("I understand this deletes everything")
    if st.button("Reset database", disabled=not confirm_reset):
        reset_database()
        st.success("Database cleared.")
        st.rerun()

    st.divider()
    st.header("Backup your library")
    st.caption("Download this whenever you add books, then re-upload it to GitHub as books_seed.csv so it survives restarts.")
    if not get_all_books().empty:
        csv_data = get_all_books()[["author", "title"]].to_csv(index=False)
        st.download_button("Download as books_seed.csv", csv_data, file_name="books_seed.csv", mime="text/csv")

    st.divider()
    st.header("Add a book manually")
    with st.form("add_book_form", clear_on_submit=True):
        new_author = st.text_input("Author")
        new_title = st.text_input("Title")
        submitted = st.form_submit_button("Add book")
        if submitted and new_author and new_title:
            add_book(new_author, new_title)
            st.success("Added.")
            st.rerun()

books_df = get_all_books()

st.header("🔖 Scan a barcode to add a book")
st.caption(
    "Point your camera at the barcode on the back of the book (not the cover art) and take a photo. "
    "This looks up the exact edition, so the cover it finds should actually match your copy."
)
barcode_photo = st.camera_input("Scan barcode", key="barcode_cam")

if barcode_photo is not None:
    isbn = extract_isbn(barcode_photo)
    if isbn is None:
        st.warning("No barcode detected in that photo — try holding the camera a bit closer and steadier.")
    else:
        info = lookup_isbn(isbn)
        if info is None:
            st.warning(f"Read barcode {isbn}, but couldn't find matching book info online. Add it manually instead.")
        else:
            cols = st.columns([1, 3])
            if info["cover_url"]:
                cols[0].image(info["cover_url"], width=120)
            cols[1].write(f"**{info['title']}**")
            cols[1].write(info["author"] or "(no author found)")
            already_exists = not books_df[
                (books_df["author"] == info["author"]) & (books_df["title"] == info["title"])
            ].empty
            if already_exists:
                cols[1].info("Already in your library.")
            elif cols[1].button("Add to library", key="add_from_barcode"):
                add_book(info["author"], info["title"])
                if info["cover_url"]:
                    updated = get_all_books()
                    match = updated[(updated["author"] == info["author"].strip()) & (updated["title"] == info["title"].strip())]
                    if not match.empty:
                        add_photo(int(match.iloc[-1]["id"]), "cover", info["cover_url"])
                st.success("Added!")
                st.rerun()

st.divider()

st.header("📷 Bulk-upload photos")
st.caption(
    "Name your files 'Author - Title.jpg' for a cover/edition photo, or "
    "'Author - Title - toc.jpg' (add toc2, toc3... for extra pages) for a table of contents. "
    "They'll be matched to the right book automatically."
)
photo_files = st.file_uploader(
    "Choose photos", type=["jpg", "jpeg", "png"], accept_multiple_files=True, key="bulk_photos"
)

if photo_files:
    needs_review = []
    auto_matched = 0
    for f in photo_files:
        author_guess, title_guess, photo_type = parse_photo_filename(f.name)
        book_id, score = find_best_match(author_guess, title_guess, books_df)
        if book_id is not None and score >= MATCH_THRESHOLD:
            url = upload_photo(f)
            add_photo(int(book_id), photo_type, url)
            auto_matched += 1
        else:
            needs_review.append((f, author_guess, title_guess, photo_type))

    if auto_matched:
        st.success(f"Auto-matched and uploaded {auto_matched} photo(s).")

    if needs_review:
        st.warning(f"{len(needs_review)} photo(s) couldn't be matched confidently — pick the book below.")
        for i, (f, author_guess, title_guess, photo_type) in enumerate(needs_review):
            cols = st.columns([1, 2, 1])
            cols[0].image(f, width=100)
            book_options = {f"{r['author']} — {r['title']}": r["id"] for _, r in books_df.iterrows()}
            choice = cols[1].selectbox(
                f"Match for '{f.name}'", ["-- select a book --"] + list(book_options.keys()), key=f"match_{i}"
            )
            type_choice = cols[2].selectbox("Type", ["cover", "toc"], index=0 if photo_type == "cover" else 1, key=f"type_{i}")
            if choice != "-- select a book --":
                if st.button("Assign", key=f"assign_{i}"):
                    url = upload_photo(f)
                    add_photo(int(book_options[choice]), type_choice, url)
                    st.success("Assigned.")
                    st.rerun()

st.divider()

if books_df.empty:
    st.info("Your library is empty. Import your Excel file from the sidebar to get started.")
else:
    st.subheader(f"{len(books_df)} books")

    col1, col2 = st.columns([2, 1])
    with col1:
        search = st.text_input("Search by title or author", "")
    with col2:
        authors = ["All authors"] + sorted(books_df["author"].unique().tolist())
        author_filter = st.selectbox("Filter by author", authors)

    filtered = books_df.copy()
    if search:
        mask = (
            filtered["title"].str.contains(search, case=False, na=False)
            | filtered["author"].str.contains(search, case=False, na=False)
        )
        filtered = filtered[mask]
    if author_filter != "All authors":
        filtered = filtered[filtered["author"] == author_filter]

    st.caption(f"Showing {len(filtered)} of {len(books_df)} books")

    for _, row in filtered.iterrows():
        book_photos = get_photos(row["id"])
        label = f"{row['author']} — {row['title']}"
        if not book_photos.empty:
            label += " 📷"
        with st.expander(label):
            c1, c2 = st.columns([5, 1])
            c1.write(f"**{row['author']}** — {row['title']}")
            if c2.button("🗑️ Delete book", key=f"del_{row['id']}"):
                delete_book(row["id"])
                st.rerun()

            if not book_photos.empty:
                photo_cols = st.columns(min(len(book_photos), 4))
                for i, (_, p) in enumerate(book_photos.iterrows()):
                    with photo_cols[i % len(photo_cols)]:
                        st.image(p["url"], caption=p["photo_type"], width=150)
                        if st.button("Remove", key=f"delphoto_{p['id']}"):
                            delete_photo(p["id"])
                            st.rerun()

    st.divider()
    with st.expander("📊 Books per author"):
        counts = books_df["author"].value_counts()
        st.bar_chart(counts)

