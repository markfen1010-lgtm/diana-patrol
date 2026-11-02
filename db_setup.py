import sqlite3
import os
import json
import hashlib
import secrets
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "monitoring.db")
MATERIALS_JSON = os.path.join(BASE_DIR, "materials.json")

def hash_password(password: str, salt: str = None) -> tuple[str, str]:
    if not salt:
        salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac(
        'sha256',
        password.encode('utf-8'),
        salt.encode('utf-8'),
        100000
    )
    return key.hex(), salt

def verify_password(password: str, salt: str, expected_hash: str) -> bool:
    key = hashlib.pbkdf2_hmac(
        'sha256',
        password.encode('utf-8'),
        salt.encode('utf-8'),
        100000
    )
    return secrets.compare_digest(key.hex(), expected_hash)

def init_database(db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # 1. Admin users table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS admin_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_login TEXT
        )
    """)

    # 2. Admin sessions table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS admin_sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            user_agent TEXT,
            ip TEXT,
            FOREIGN KEY (user_id) REFERENCES admin_users(id) ON DELETE CASCADE
        )
    """)

    # 3. AI usage logs (rate limiting & tracking)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ai_usage_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_hash TEXT NOT NULL,
            assistant_type TEXT NOT NULL,
            prompt_preview TEXT,
            tokens_prompt INTEGER DEFAULT 0,
            tokens_resp INTEGER DEFAULT 0,
            status TEXT DEFAULT 'SUCCESS',
            created_at TEXT NOT NULL
        )
    """)

    # 4. AI config table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ai_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

    # 5. RAG documents table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS rag_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_num INTEGER,
            title TEXT NOT NULL,
            doc_type TEXT,
            speaker TEXT,
            part INTEGER DEFAULT 1,
            timecode TEXT,
            video_link TEXT,
            prosecution_purpose TEXT,
            defense_counter TEXT,
            defense_evidence TEXT,
            action_proposal TEXT,
            full_content TEXT,
            indexed_at TEXT NOT NULL
        )
    """)

    # Default settings
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    defaults = {
        "daily_limit_per_user": "20",
        "analyst_model": "gemini-3.5-flash",
        "guide_model": "gemini-flash-lite-latest",
        "ai_enabled": "true",
        "max_output_tokens": "2048",
        "request_timeout": "30"
    }
    for k, v in defaults.items():
        cur.execute("""
            INSERT OR IGNORE INTO ai_config (key, value, updated_at)
            VALUES (?, ?, ?)
        """, (k, v, now_str))

    # Check admin user
    cur.execute("SELECT COUNT(*) FROM admin_users")
    if cur.fetchone()[0] == 0:
        # Initial admin user
        # In production, can be set via env ADMIN_PASSWORD or changed in admin panel
        admin_pass = os.environ.get("ADMIN_PASSWORD", "123456789A")
        p_hash, p_salt = hash_password(admin_pass)
        cur.execute("""
            INSERT INTO admin_users (username, password_hash, salt, created_at)
            VALUES (?, ?, ?, ?)
        """, ("admin", p_hash, p_salt, now_str))
        print(f"[DB Setup] Created default admin user 'admin'.")

    # Index RAG documents if empty
    cur.execute("SELECT COUNT(*) FROM rag_documents")
    rag_count = cur.fetchone()[0]
    if rag_count == 0 and os.path.exists(MATERIALS_JSON):
        try:
            with open(MATERIALS_JSON, "r", encoding="utf-8") as f:
                materials = json.load(f)
            
            for item in materials:
                doc_num = item.get("num")
                title = item.get("name") or f"Документ №{doc_num}"
                doc_type = item.get("type", "")
                speaker = item.get("speaker", "")
                part = item.get("part", 1)
                timecode = item.get("timecode", "")
                vlink = item.get("video_link", "")
                pros = item.get("prosecution_purpose", "")
                def_cntr = item.get("defense_counter", "")
                def_evid = item.get("defense_evidence", "")
                act_prop = item.get("action_proposal", "")

                full_text = f"""ДОКУМЕНТ СПРАВИ: Протокол/Матеріал №{doc_num}.
НАЗВА: {title}
ТИП: {doc_type}
ХТО ОГОЛОШУЄ / ДОПОВІДАЧ: {speaker}
СУДОВЕ ЗАСІДАННЯ ВАКС: Частина {part}, таймкод {timecode}
МЕТА ОБВИНУВАЧЕННЯ (САП / НАБУ): {pros}
КОНТРАРГУМЕНТ ЗАХИСТУ: {def_cntr}
ДОКАЗИ ТА ПІДТВЕРДЖЕННЯ ЗАХИСТУ: {def_evid}
ПРОЦЕСУАЛЬНА ДІЯ / КЛОПОТАННЯ АДВОКАТА: {act_prop}
""".strip()

                cur.execute("""
                    INSERT INTO rag_documents (
                        doc_num, title, doc_type, speaker, part, timecode,
                        video_link, prosecution_purpose, defense_counter,
                        defense_evidence, action_proposal, full_content, indexed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    doc_num, title, doc_type, speaker, part, timecode,
                    vlink, pros, def_cntr, def_evid, act_prop, full_text, now_str
                ))

            # Also add summary document for VAKS Court Hearings (Parts 1 & 2)
            vaks_summary = """СУДОВЕ ПРОВАДЖЕННЯ: Вищий антикорупційний суд (ВАКС).
СПРАВА: № 991/10659/26 щодо підозрюваної Мудрої Ірини Романівни.
ПРЕДМЕТ РОЗГЛЯДУ: Клопотання детективів НАБУ та прокурорів САП про застосування запобіжного заходу у вигляді тримання під вартою з альтернативою застави 150 000 000 грн.
ЗАСІДАННЯ ЧАСТИНА 1: Розгляд першої частини доказів обвинувачення (протоколи огляду телефонів, листування, вилучені документи). Позиція захисту: повна необґрунтованість підозри, відсутність збитків, відсутність ризиків переховування.
ЗАСІДАННЯ ЧАСТИНА 2: Дебати сторін та дослідження характеристик. Адвокати надали докази законності дій, характеристику з роботи, підтвердження відсутності закордонних активів.
СУДОВЕ РІШЕННЯ: Слідчий суддя ВАКС повністю відмовив прокурору САП у заставі 150 млн грн, зменшивши її в 7,5 разів до 20 млн грн.""".strip()

            cur.execute("""
                INSERT INTO rag_documents (
                    doc_num, title, doc_type, speaker, part, timecode,
                    video_link, prosecution_purpose, defense_counter,
                    defense_evidence, action_proposal, full_content, indexed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                100, "Огляд судового розгляду ВАКС (Справа 991/10659/26)", "Судовий акт / Огляд", "Слідчий суддя ВАКС",
                1, "00:00:00", "https://youtube.com", "Обрання тримання під вартою / застави 150 млн грн",
                "Зменшення застави, відсутність ризиків, штучність звинувачення",
                "Характеристики, довідки про доходи, відсутність майна за кордоном",
                "Відмова у клопотанні прокурора у повному обсязі", vaks_summary, now_str
            ))

            print(f"[DB Setup] Indexed {len(materials) + 1} RAG documents.")
        except Exception as e:
            print(f"[DB Setup] Error indexing materials: {e}")

    conn.commit()
    conn.close()
    print("[DB Setup] Database initialization complete.")

if __name__ == "__main__":
    init_database()
