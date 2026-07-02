import datetime
import hashlib
import json
import time
from pathlib import Path

from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_core.prompts import PromptTemplate
from langchain_google_genai import (
    ChatGoogleGenerativeAI,
    GoogleGenerativeAIEmbeddings,
    HarmBlockThreshold,
    HarmCategory,
)
from langchain_text_splitters import MarkdownHeaderTextSplitter

from .export import ARCHIVED_LOGS_DIR, DATA_DIR, LIVE_LOGS_DIR, REPO_DIR

CHROMA_DIR = DATA_DIR / "chroma_db"
CACHE_FILE = DATA_DIR / "sync_cache.json"


def load_cache():
    if CACHE_FILE.exists():
        with open(CACHE_FILE, "r") as f:
            return json.load(f)
    return {"docs": {}, "threads": {}, "chat": {}}


def save_cache(cache):
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f)


def get_hash(content: str) -> str:
    return hashlib.md5(content.encode("utf-8")).hexdigest()


def get_embeddings():
    return GoogleGenerativeAIEmbeddings(model="models/gemini-embedding-2")


def retry_api_call(func, *args, **kwargs):
    max_retries = 8
    base_delay = 5
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            err_str = str(e).lower()
            transient_keywords = [
                "429",
                "resource_exhausted",
                "quota",
                "disconnect",
                "timeout",
                "500",
                "502",
                "503",
                "504",
                "connection",
            ]
            if any(k in err_str for k in transient_keywords):
                delay = base_delay * (2**attempt)
                print(
                    f"Transient API issue hit. Retrying in {delay}s (Attempt {attempt + 1}/{max_retries})... Error: {e}"
                )
                time.sleep(delay)
            else:
                raise e
    raise Exception("Max retries exceeded for API call.")


def get_llm():
    return ChatGoogleGenerativeAI(
        model="gemini-2.5-flash",
        temperature=0,
        safety_settings={
            HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_ONLY_HIGH,
            HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_ONLY_HIGH,
            HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_ONLY_HIGH,
            HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_ONLY_HIGH,
        },
    )


MARKDOWN_PROCESSOR_VERSION = 1


def ingest_docs(vectorstore, cache):
    print("Ingesting markdown documentation...")
    docs_dir = REPO_DIR / "mastercomfig" / "docs"

    if not docs_dir.exists():
        print(f"Docs directory {docs_dir} not found.")
        return False

    headers_to_split_on = [
        ("#", "Header 1"),
        ("##", "Header 2"),
        ("###", "Header 3"),
    ]
    markdown_splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=headers_to_split_on
    )

    docs_added = False
    for md_file in docs_dir.rglob("*.md"):
        with open(md_file, "r", encoding="utf-8") as f:
            content = f.read()

            version_str = (
                "_v" + str(MARKDOWN_PROCESSOR_VERSION)
                if MARKDOWN_PROCESSOR_VERSION > 0
                else ""
            )
            content_hash = get_hash(content + version_str)
            file_key = str(md_file.relative_to(docs_dir))

            if cache["docs"].get(file_key) == content_hash:
                continue
            splits = markdown_splitter.split_text(content)
            valid_splits = []
            for split in splits:
                if split.page_content.strip():
                    split.metadata["source"] = file_key
                    split.metadata["type"] = "documentation"
                    split.metadata["created_at"] = 9999999999  # Far future for priority
                    valid_splits.append(split)

            if valid_splits:
                for idx, doc in enumerate(valid_splits):
                    doc_id = f"doc_{file_key}_{idx}"
                    try:
                        retry_api_call(vectorstore.add_documents, [doc], ids=[doc_id])
                    except Exception as e:
                        print(
                            f"Failed to ingest document: {doc.page_content[:50]}... Error: {e}"
                        )
                        time.sleep(2)
                cache["docs"][file_key] = content_hash
                save_cache(cache)
                docs_added = True
    return docs_added


THREAD_PROCESSOR_VERSION = 3


def summarize_thread(thread_name: str, messages: list) -> str:
    llm = get_llm()
    content = f"Thread Topic: {thread_name}\n"
    for m in messages:
        content += f"[{m['author']}]: {m['content']}\n"

    prompt = PromptTemplate.from_template(
        "You are an expert technical support engineer. Read the following Discord support thread and extract the problem and the accepted or most helpful answer. "
        "Keep the summary concise and focused on the technical solution. If no clear solution is found, just summarize the problem.\n\n"
        "Thread:\n{thread}\n\nSummary:"
    )
    chain = prompt | llm
    try:
        response = retry_api_call(chain.invoke, {"thread": content})
        return response.content
    except Exception as e:
        print(f"Error summarizing thread {thread_name}: {e}")
        return content[:2000]


LOG_PROCESSOR_VERSION = 1


def process_log_file(file_path: Path, vectorstore, cache):
    print(f"Processing log file {file_path.name}...")
    with open(file_path, "r", encoding="utf-8") as f:
        try:
            messages = json.load(f)
        except json.JSONDecodeError:
            return

    threads = {}
    for msg in messages:
        tid = msg.get("thread_id")
        if tid:
            if tid not in threads:
                threads[tid] = []
            threads[tid].append(msg)

    for tid, tmsgs in threads.items():
        thread_name = tmsgs[0].get("thread_name", "Unknown Thread")

        thread_content = json.dumps(tmsgs)
        version_str = (
            "_v" + str(THREAD_PROCESSOR_VERSION) if THREAD_PROCESSOR_VERSION > 0 else ""
        )
        thread_hash = get_hash(thread_content + version_str)

        if cache["threads"].get(str(tid)) == thread_hash:
            continue

        try:
            created_at_str = tmsgs[0].get("created_at")
            dt = datetime.datetime.fromisoformat(created_at_str)
            created_at_ts = int(dt.timestamp())
        except Exception:
            created_at_ts = 0

        summary = summarize_thread(thread_name, tmsgs)
        if summary.strip():
            doc = Document(
                page_content=f"Problem and Solution from '{thread_name}':\n{summary}",
                metadata={
                    "source": file_path.name,
                    "type": "forum_thread",
                    "thread_id": str(tid),
                    "created_at": created_at_ts,
                },
            )
            try:
                doc_id = f"thread_{tid}"
                retry_api_call(vectorstore.add_documents, [doc], ids=[doc_id])
            except Exception as e:
                print(f"Failed to ingest thread {tid}: {e}")
                time.sleep(2)
                continue
        else:
            print(f"Warning: Thread {tid} ({thread_name}) yielded an empty summary. Caching it to avoid repeating LLM call.")

        cache["threads"][str(tid)] = thread_hash
        save_cache(cache)

    text_msgs = [m for m in messages if not m.get("thread_id")]
    chunk_size = 50
    for i in range(0, len(text_msgs), chunk_size):
        chunk = text_msgs[i : i + chunk_size]
        content = ""
        for m in chunk:
            content += f"[{m['author']}]: {m['content']}\n"
        content = content.strip()
        if content:
            chunk_id = f"chat_{file_path.name}_{i}"
            version_str = (
                "_v" + str(LOG_PROCESSOR_VERSION) if LOG_PROCESSOR_VERSION > 0 else ""
            )
            chunk_hash = get_hash(content + version_str)

            if cache["chat"].get(chunk_id) == chunk_hash:
                continue

            try:
                last_msg_time = chunk[-1].get("created_at")
                dt = datetime.datetime.fromisoformat(last_msg_time)
                created_at_ts = int(dt.timestamp())
            except Exception:
                created_at_ts = 0

            doc = Document(
                page_content=content,
                metadata={
                    "source": file_path.name,
                    "type": "chat_history",
                    "created_at": created_at_ts,
                },
            )
            try:
                retry_api_call(vectorstore.add_documents, [doc], ids=[chunk_id])
                cache["chat"][chunk_id] = chunk_hash
                save_cache(cache)
            except Exception as e:
                print(f"Failed to ingest chat chunk: {e}")
                time.sleep(2)


RELEASE_PROCESSOR_VERSION = 1


def process_github_releases(vectorstore, cache):
    releases_file = Path("rag_data/github_releases.json")
    if not releases_file.exists():
        return

    print("Ingesting GitHub releases...")
    with open(releases_file, "r", encoding="utf-8") as f:
        try:
            releases = json.load(f)
        except json.JSONDecodeError:
            return

    if "releases" not in cache:
        cache["releases"] = {}

    headers_to_split_on = [
        ("#", "Header 1"),
        ("##", "Header 2"),
        ("###", "Header 3"),
    ]
    markdown_splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=headers_to_split_on
    )

    for rel in releases:
        tag_name = rel.get("tag_name", "unknown")
        body = rel.get("body", "")
        if not body.strip():
            continue

        content = f"Release {tag_name}:\n\n{body}"
        version_str = "_v" + str(RELEASE_PROCESSOR_VERSION)
        rel_hash = get_hash(content + version_str)

        rel_id = f"release_{tag_name}"
        if cache["releases"].get(rel_id) == rel_hash:
            continue

        try:
            dt = datetime.datetime.fromisoformat(
                rel.get("published_at").replace("Z", "+00:00")
            )
            created_at_ts = int(dt.timestamp())
        except Exception:
            created_at_ts = 0

        splits = markdown_splitter.split_text(content)

        valid_splits = []
        for split in splits:
            if split.page_content.strip():
                split.metadata["source"] = f"release_{tag_name}"
                split.metadata["type"] = "release_note"
                split.metadata["created_at"] = created_at_ts
                valid_splits.append(split)

        if valid_splits:
            ids = [f"{rel_id}_{i}" for i in range(len(valid_splits))]
            for doc in valid_splits:
                try:
                    retry_api_call(vectorstore.add_documents, [doc], ids=ids)
                    cache["releases"][rel_id] = rel_hash
                    save_cache(cache)
                except Exception as e:
                    print(f"Failed to ingest release {tag_name}: {e}")
                    time.sleep(2)


def ingest_all(force_docs=False, force_logs=False):
    CHROMA_DIR.parent.mkdir(exist_ok=True)
    vectorstore = Chroma(
        persist_directory=str(CHROMA_DIR), embedding_function=get_embeddings()
    )

    cache = load_cache()

    ingest_docs(vectorstore, cache)
    process_github_releases(vectorstore, cache)

    for log_dir in [ARCHIVED_LOGS_DIR, LIVE_LOGS_DIR]:
        if log_dir.exists():
            for log_file in log_dir.glob("*.json"):
                process_log_file(log_file, vectorstore, cache)

    save_cache(cache)

    print("RAG ingest job completed.")
