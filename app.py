import sqlite3
from pathlib import Path

import pandas as pd
import streamlit as st

DB_PATH = Path(__file__).parent / "library.db"

st.set_page_config(page_title="My Library", page_icon="📚", layout="wide")


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
    conn.commit()
    conn.close()


def get_all_books() -> pd.DataFrame:
    conn = get_connection()
    df = pd.read_sql_query("SELECT id, author, title FROM books ORDER BY author, title", conn)
    conn.close()
    return df


def add_book(author: str, title: str):
    conn = get_connection()
    conn.execute("INSERT INTO books (author, title) VALUES (?, ?)", (author.strip(), title.strip()))
    conn.commit()
    conn.close()


def delete_book(book_id: int):
    conn = get_connection()
    conn.execute("DELETE FROM books WHERE id = ?", (book_id,))
    conn.commit()
    conn.close()


def import_excel(file, has_header: bool):
    """Reads an Excel file with two columns (author, title) and inserts rows into the DB."""
    header_arg = 0 if has_header else None
    df = pd.read_excel(file, header=header_arg)
    df = df.iloc[:, :2]
    df.columns = ["author", "title"]
    df = df.dropna(how="all")

    conn = get_connection()
    df.to_sql("books_import_tmp", conn, if_exists="replace", index=False)
    conn.execute("INSERT INTO books (author, title) SELECT author, title FROM books_import_tmp")
    conn.execute("DROP TABLE books_import_tmp")
    conn.commit()
    conn.close()
    return len(df)


def book_count() -> int:
    conn = get_connection()
    count = conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
    conn.close()
    return count


# ---------- App ----------

init_db()

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
        c1, c2, c3 = st.columns([3, 5, 1])
        c1.write(row["author"])
        c2.write(row["title"])
        if c3.button("🗑️", key=f"del_{row['id']}"):
            delete_book(row["id"])
            st.rerun()

    st.divider()
    with st.expander("📊 Books per author"):
        counts = books_df["author"].value_counts()
        st.bar_chart(counts)
