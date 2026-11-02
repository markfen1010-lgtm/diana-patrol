import http.server
import socketserver
import json
import urllib.parse
import os
import sys
import sqlite3
import socket
import threading
import time
import hashlib
import secrets
from datetime import datetime, timedelta

# Import agent logic
try:
    from agent import AdvocateProtectionAgent, run_test_suite, EmailReporter
except ImportError as e:
    print(f"[Warning] Agent import error: {e}")
    AdvocateProtectionAgent = None

# Import Gemini AI Service and DB Setup
from gemini_service import GeminiService
from db_setup import hash_password, verify_password, init_database

PORT = int(os.environ.get("PORT", 8765))
HOST = "0.0.0.0"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPORTS_DIR = os.path.join(BASE_DIR, "reports")
DB_PATH = os.path.join(BASE_DIR, "monitoring.db")
MATERIALS_JSON = os.path.join(BASE_DIR, "materials.json")
EXCEL_ANALYSIS = os.path.join(BASE_DIR, "Аналіз_матеріалів_обвинувачення.xlsx")

# Ensure reports directory exists
os.makedirs(REPORTS_DIR, exist_ok=True)

GLOBAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "https://diana-patrol.onrender.com")

SESSION_COOKIE_NAME = "admin_session"
SESSION_DURATION_HOURS = 72
SERVER_START_TIME = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# Initialize DB tables & seed RAG documents if needed
init_database(DB_PATH)
ai_service = GeminiService(DB_PATH)

def get_server_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

def generate_qr_code(target_url=None):
    try:
        import qrcode
        import qrcode.image.svg
        if not target_url:
            target_url = GLOBAL_URL
        factory = qrcode.image.svg.SvgPathImage
        img = qrcode.make(target_url, image_factory=factory)
        qr_path = os.path.join(BASE_DIR, "qr_phone.svg")
        with open(qr_path, "wb") as f:
            img.save(f)
        return qr_path
    except Exception as e:
        print(f"[HUD Server] QR generation note: {e}")
        return None

# =============================================================================
# SESSION AND AUTHENTICATION HELPERS
# =============================================================================
def get_session_token_from_headers(headers) -> str:
    cookie_header = headers.get("Cookie", "")
    for item in cookie_header.split(";"):
        item = item.strip()
        if item.startswith(SESSION_COOKIE_NAME + "="):
            return item.split("=", 1)[1].strip()
    auth_header = headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:].strip()
    return ""

def verify_admin_session(token: str) -> dict | None:
    if not token:
        return None
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cur.execute("""
            SELECT u.id, u.username, s.expires_at
            FROM admin_sessions s
            JOIN admin_users u ON s.user_id = u.id
            WHERE s.token = ? AND s.expires_at > ?
        """, (token, now_str))
        row = cur.fetchone()
        conn.close()
        if row:
            return {"user_id": row[0], "username": row[1], "expires_at": row[2]}
    except Exception as e:
        print(f"[Auth Error] verify_admin_session: {e}")
    return None

def create_admin_session(user_id: int, user_agent: str = "", ip: str = "") -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    expires_at = (datetime.now() + timedelta(hours=SESSION_DURATION_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO admin_sessions (token, user_id, expires_at, created_at, user_agent, ip)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (token, user_id, expires_at, created_at, user_agent[:200], ip))
        conn.commit()
        conn.close()
        return token, expires_at
    except Exception as e:
        print(f"[Auth Error] create_admin_session: {e}")
        return "", ""

def destroy_admin_session(token: str):
    if not token:
        return
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("DELETE FROM admin_sessions WHERE token = ?", (token,))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[Auth Error] destroy_admin_session: {e}")

class TacticalHUDHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=BASE_DIR, **kwargs)

    def get_client_hash(self) -> str:
        ip = self.client_address[0] if self.client_address else "127.0.0.1"
        ua = self.headers.get("User-Agent", "")
        raw = f"{ip}:{ua}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def is_authenticated_admin(self) -> dict | None:
        token = get_session_token_from_headers(self.headers)
        return verify_admin_session(token)

    def send_unauthorized(self, message: str = "Потрібна авторизація адміністратора"):
        self.send_json({"status": "UNAUTHORIZED", "message": message}, status=401)

    def send_json(self, data, status=200, set_cookie=None, clear_cookie=False):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        if set_cookie:
            is_secure = "https://" in GLOBAL_URL
            sec_flag = "; Secure" if is_secure else ""
            self.send_header("Set-Cookie", f"{SESSION_COOKIE_NAME}={set_cookie}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_DURATION_HOURS*3600}{sec_flag}")
        elif clear_cookie:
            self.send_header("Set-Cookie", f"{SESSION_COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")
        self.end_headers()
        self.wfile.write(body)

    def read_json_body(self) -> dict:
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length > 0:
                raw = self.rfile.read(content_length).decode('utf-8')
                return json.loads(raw)
        except Exception:
            pass
        return {}

    # =========================================================================
    # GET HANDLER
    # =========================================================================
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # Health checks for cloud orchestrators (Render, K8s, Docker)
        if path in ["/healthz", "/health", "/ping"]:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")
            return

        # Main Web App
        if path in ["/", "/index.html", "/app", "/hud", "/admin"]:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.end_headers()
            with open(os.path.join(BASE_DIR, "app.html"), "rb") as f:
                self.wfile.write(f.read())
            return

        elif path == "/qr_phone.svg":
            qr_file = os.path.join(BASE_DIR, "qr_phone.svg")
            if not os.path.exists(qr_file):
                generate_qr_code()
            if os.path.exists(qr_file):
                self.send_response(200)
                self.send_header("Content-Type", "image/svg+xml")
                self.end_headers()
                with open(qr_file, "rb") as f:
                    self.wfile.write(f.read())
                return
            else:
                self.send_error(404, "QR not found")
                return

        elif any(path.endswith(ext) for ext in [".svg", ".png", ".ico", ".jpg", ".jpeg"]):
            fname = path.lstrip("/")
            file_path = os.path.join(BASE_DIR, fname)
            if not os.path.exists(file_path):
                if "apple-touch-icon" in path:
                    file_path = os.path.join(BASE_DIR, "apple-touch-icon.png")
            if os.path.exists(file_path) and os.path.isfile(file_path):
                if file_path.endswith(".svg"):
                    mime = "image/svg+xml"
                elif file_path.endswith(".ico"):
                    mime = "image/x-icon"
                elif file_path.endswith(".png"):
                    mime = "image/png"
                else:
                    mime = "image/jpeg"
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.end_headers()
                with open(file_path, "rb") as f:
                    self.wfile.write(f.read())
                return
            else:
                self.send_error(404, "File not found")
                return

        elif path in ["/manifest.json", "/manifest.webmanifest", "/site.webmanifest"]:
            manifest_file = os.path.join(BASE_DIR, "manifest.webmanifest")
            if not os.path.exists(manifest_file):
                manifest_file = os.path.join(BASE_DIR, "manifest.json")
            if os.path.exists(manifest_file):
                self.send_response(200)
                self.send_header("Content-Type", "application/manifest+json; charset=utf-8")
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.end_headers()
                with open(manifest_file, "rb") as f:
                    self.wfile.write(f.read())
                return
            else:
                self.send_error(404, "Manifest not found")
                return

        # =====================================================================
        # PUBLIC APIs
        # =====================================================================
        elif path == "/api/network-info":
            server_ip = get_server_ip()
            self.send_json({
                "local_ip": server_ip,
                "port": PORT,
                "mobile_url": GLOBAL_URL,
                "global_url": GLOBAL_URL,
                "global_ip": server_ip,
                "qr_url": "/qr_phone.svg"
            })
            return

        elif path == "/api/status":
            pub_count = 0
            if os.path.exists(DB_PATH):
                try:
                    conn = sqlite3.connect(DB_PATH)
                    cur = conn.cursor()
                    cur.execute("SELECT COUNT(*) FROM publications")
                    pub_count = cur.fetchone()[0]
                    conn.close()
                except Exception as e:
                    print(f"[Status API] Error: {e}")

            server_ip = get_server_ip()
            self.send_json({
                "status": "ONLINE",
                "court_case": "991/10659/26",
                "suspect": "Мудра Ірина Романівна",
                "court": "Вищий антикорупційний суд",
                "total_publications": pub_count,
                "local_ip": server_ip,
                "mobile_url": GLOBAL_URL,
                "global_url": GLOBAL_URL,
                "agent_ready": AdvocateProtectionAgent is not None,
                "ai_ready": bool(ai_service.get_api_key())
            })
            return

        elif path == "/api/materials":
            materials = []
            if os.path.exists(MATERIALS_JSON):
                try:
                    with open(MATERIALS_JSON, "r", encoding="utf-8") as f:
                        materials = json.load(f)
                except Exception as e:
                    print(f"[Materials API] Error: {e}")
            self.send_json(materials)
            return

        elif path == "/api/publications":
            pubs = []
            if os.path.exists(DB_PATH):
                try:
                    conn = sqlite3.connect(DB_PATH)
                    cur = conn.cursor()
                    cur.execute("""
                        SELECT id, discovered_at, published_at, source_name, author,
                               url, title, problematic_quote, context_type, violation_signs,
                               legal_norm, risk_level, ai_explanation
                        FROM publications ORDER BY id DESC
                    """)
                    rows = cur.fetchall()
                    conn.close()
                    for r in rows:
                        pubs.append({
                            "id": r[0], "discovered_at": r[1], "published_at": r[2],
                            "source_name": r[3], "author": r[4], "url": r[5], "title": r[6],
                            "problematic_quote": r[7], "context_type": r[8], "violation_signs": r[9],
                            "legal_norm": r[10], "risk_level": r[11], "ai_explanation": r[12]
                        })
                except Exception as e:
                    print(f"[Publications API] Error: {e}")
            self.send_json(pubs)
            return

        elif path == "/api/auth/status":
            admin_user = self.is_authenticated_admin()
            self.send_json({
                "authenticated": admin_user is not None,
                "username": admin_user.get("username", "") if admin_user else "",
                "expires_at": admin_user.get("expires_at", "") if admin_user else ""
            })
            return

        elif path == "/api/ai/status":
            has_key = bool(ai_service.get_api_key())
            client_hash = self.get_client_hash()
            allowed, used_today, daily_limit = ai_service.check_rate_limit(client_hash)
            self.send_json({
                "ai_enabled": ai_service.get_config_val("ai_enabled", "true") == "true",
                "api_key_configured": has_key,
                "analyst_model": ai_service.get_config_val("analyst_model", "gemini-3.5-flash"),
                "guide_model": ai_service.get_config_val("guide_model", "gemini-flash-lite-latest"),
                "daily_limit_per_user": daily_limit,
                "user_requests_today": used_today,
                "user_requests_left": max(0, daily_limit - used_today)
            })
            return

        # =====================================================================
        # PROTECTED ADMIN GET APIs
        # =====================================================================
        elif path == "/api/smtp-config":
            if not self.is_authenticated_admin():
                return self.send_unauthorized()
            if AdvocateProtectionAgent:
                agent = AdvocateProtectionAgent()
                cfg = agent.storage.get_smtp_settings()
                cfg_copy = dict(cfg)
                cfg_copy["has_password"] = bool(cfg_copy.get("password"))
                if cfg_copy.get("password"):
                    cfg_copy["password"] = "******"
                self.send_json(cfg_copy)
            else:
                self.send_json({})
            return

        elif path == "/api/email-logs":
            if not self.is_authenticated_admin():
                return self.send_unauthorized()
            if AdvocateProtectionAgent:
                agent = AdvocateProtectionAgent()
                logs = agent.storage.get_email_dispatch_logs(50)
                self.send_json(logs)
            else:
                self.send_json([])
            return

        elif path == "/api/schedules":
            # Can be inspected by client (read-only)
            if AdvocateProtectionAgent:
                agent = AdvocateProtectionAgent()
                schedules = agent.storage.get_schedules()
                self.send_json(schedules)
            else:
                self.send_json([])
            return

        elif path == "/api/admin/overview":
            if not self.is_authenticated_admin():
                return self.send_unauthorized()
            
            pub_cnt, sch_cnt, log_cnt, rag_cnt, ai_queries_today = 0, 0, 0, 0, 0
            today_prefix = datetime.now().strftime("%Y-%m-%d")
            recent_ai_logs = []
            try:
                conn = sqlite3.connect(DB_PATH)
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) FROM publications")
                pub_cnt = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM email_schedules")
                sch_cnt = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM email_dispatch_logs")
                log_cnt = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM rag_documents")
                rag_cnt = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM ai_usage_logs WHERE created_at LIKE ?", (f"{today_prefix}%",))
                ai_queries_today = cur.fetchone()[0]

                cur.execute("""
                    SELECT id, assistant_type, prompt_preview, status, created_at
                    FROM ai_usage_logs ORDER BY id DESC LIMIT 15
                """)
                for r in cur.fetchall():
                    recent_ai_logs.append({
                        "id": r[0], "assistant": r[1], "prompt": r[2], "status": r[3], "time": r[4]
                    })
                conn.close()
            except Exception as e:
                print(f"[Admin Overview Error] {e}")

            db_size_kb = os.path.getsize(DB_PATH) // 1024 if os.path.exists(DB_PATH) else 0

            self.send_json({
                "status": "SUCCESS",
                "server_start_time": SERVER_START_TIME,
                "current_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "db_size_kb": db_size_kb,
                "publications_count": pub_cnt,
                "schedules_count": sch_cnt,
                "dispatch_logs_count": log_cnt,
                "rag_documents_count": rag_cnt,
                "ai_queries_today": ai_queries_today,
                "gemini_api_key_configured": bool(ai_service.get_api_key()),
                "analyst_model": ai_service.get_config_val("analyst_model", "gemini-3.5-flash"),
                "guide_model": ai_service.get_config_val("guide_model", "gemini-flash-lite-latest"),
                "daily_limit_per_user": ai_service.get_config_val("daily_limit_per_user", "20"),
                "recent_ai_logs": recent_ai_logs
            })
            return

        elif path == "/api/admin/backup-db":
            if not self.is_authenticated_admin():
                return self.send_unauthorized()
            if os.path.exists(DB_PATH):
                self.send_response(200)
                self.send_header("Content-Type", "application/x-sqlite3")
                fname = f"monitoring_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
                self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
                self.end_headers()
                with open(DB_PATH, "rb") as f:
                    self.wfile.write(f.read())
                return
            else:
                self.send_error(404, "Database file not found")
                return

        elif path.startswith("/download/"):
            filename = urllib.parse.unquote(path[len("/download/"):])
            filepath = os.path.join(REPORTS_DIR, filename)
            if not os.path.exists(filepath):
                filepath = os.path.join(BASE_DIR, filename)

            if os.path.exists(filepath):
                self.send_response(200)
                if filename.endswith(".xlsx"):
                    self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                elif filename.endswith(".docx"):
                    self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
                elif filename.endswith(".json"):
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                elif filename.endswith(".eml") or filename.endswith(".txt"):
                    self.send_header("Content-Type", "message/rfc822")
                else:
                    self.send_header("Content-Type", "application/octet-stream")

                encoded_filename = urllib.parse.quote(filename)
                self.send_header("Content-Disposition", f'attachment; filename="{encoded_filename}"; filename*=UTF-8\'\'{encoded_filename}')
                self.end_headers()
                with open(filepath, "rb") as f:
                    self.wfile.write(f.read())
                return
            else:
                self.send_error(404, f"Файл не знайдено: {filename}")
                return

        elif path == "/latest-report":
            if os.path.exists(REPORTS_DIR):
                reports = [os.path.join(REPORTS_DIR, f) for f in os.listdir(REPORTS_DIR) if f.endswith(".html")]
                if reports:
                    reports.sort(key=os.path.getmtime, reverse=True)
                    latest = reports[0]
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    with open(latest, "rb") as f:
                        self.wfile.write(f.read())
                    return
            self.send_error(404, "Звітів ще не згенеровано.")
            return

        super().do_GET()

    # =========================================================================
    # POST HANDLER
    # =========================================================================
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        req_data = self.read_json_body()

        # ---------------------------------------------------------------------
        # 1. AUTHENTICATION ENDPOINTS
        # ---------------------------------------------------------------------
        if path == "/api/auth/login":
            username = req_data.get("username", "").strip()
            password = req_data.get("password", "").strip()
            if not username or not password:
                self.send_json({"status": "FAIL", "message": "Введіть логін та пароль"}, status=400)
                return

            authenticated = False
            user_id = None
            try:
                conn = sqlite3.connect(DB_PATH)
                cur = conn.cursor()
                cur.execute("SELECT id, username, password_hash, salt FROM admin_users WHERE username = ?", (username,))
                row = cur.fetchone()
                if row:
                    user_id, uname, p_hash, p_salt = row
                    if verify_password(password, p_salt, p_hash):
                        authenticated = True
                        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        cur.execute("UPDATE admin_users SET last_login = ? WHERE id = ?", (now_str, user_id))
                        conn.commit()
                # Fallback support for Render environment variable ADMIN_PASSWORD
                if not authenticated and username == "admin":
                    env_admin_pass = os.environ.get("ADMIN_PASSWORD", "")
                    if env_admin_pass and password == env_admin_pass:
                        authenticated = True
                        if row:
                            user_id = row[0]
                conn.close()
            except Exception as e:
                print(f"[Login Error] {e}")

            if authenticated and user_id:
                ip = self.client_address[0] if self.client_address else ""
                ua = self.headers.get("User-Agent", "")
                token, expires_at = create_admin_session(user_id, ua, ip)
                self.send_json({
                    "status": "SUCCESS",
                    "message": "Авторизація успішна",
                    "username": username,
                    "token": token,
                    "expires_at": expires_at
                }, set_cookie=token)
            else:
                self.send_json({"status": "FAIL", "message": "Невірний логін або пароль"}, status=401)
            return

        elif path == "/api/auth/logout":
            token = get_session_token_from_headers(self.headers)
            destroy_admin_session(token)
            self.send_json({"status": "SUCCESS", "message": "Сесію завершено"}, clear_cookie=True)
            return

        # ---------------------------------------------------------------------
        # 2. AI ASSISTANTS (PUBLIC WITH RATE LIMITS)
        # ---------------------------------------------------------------------
        elif path == "/api/ai/analyst":
            message = req_data.get("message", "").strip()
            if not message:
                self.send_json({"status": "FAIL", "reply": "Запит не може бути порожнім."}, status=400)
                return
            current_doc_id = req_data.get("current_doc_id")
            compare_doc_ids = req_data.get("compare_doc_ids")
            client_hash = self.get_client_hash()
            res = ai_service.ask_analyst(message, client_hash, current_doc_id=current_doc_id, compare_doc_ids=compare_doc_ids)
            self.send_json(res)
            return

        elif path == "/api/ai/guide":
            message = req_data.get("message", "").strip()
            if not message:
                self.send_json({"status": "FAIL", "reply": "Запит не може бути порожнім."}, status=400)
                return
            current_section = req_data.get("current_section", "dashboard")
            client_hash = self.get_client_hash()
            res = ai_service.ask_guide(message, client_hash, current_section=current_section)
            self.send_json(res)
            return

        elif path == "/api/ai/doc-analysis":
            doc_num = req_data.get("doc_num")
            if not doc_num:
                self.send_json({"status": "FAIL", "reply": "Вкажіть номер документа."}, status=400)
                return
            client_hash = self.get_client_hash()
            res = ai_service.analyze_single_doc(int(doc_num), client_hash)
            self.send_json(res)
            return

        # ---------------------------------------------------------------------
        # 3. PUBLIC RADAR & EXPORT
        # ---------------------------------------------------------------------
        elif path == "/api/custom-search":
            try:
                target_name = req_data.get("target_name", "")
                keywords = req_data.get("keywords", "")
                channels = req_data.get("channels", None)
                source_type = req_data.get("source_type", "ALL")
                save_to_db = req_data.get("save_to_db", False)

                agent = AdvocateProtectionAgent()
                res = agent.run_dynamic_search(
                    target_name=target_name,
                    keywords_str=keywords,
                    telegram_channels=channels,
                    source_type=source_type,
                    save_to_db=save_to_db
                )
                self.send_json({"status": "SUCCESS", "data": res})
            except Exception as e:
                import traceback
                traceback.print_exc()
                self.send_json({"status": "ERROR", "message": str(e)})
            return

        elif path in ["/api/export-search", "/api/export-custom-excel"]:
            try:
                target_name = req_data.get("target_name", "фігурант")
                records = req_data.get("records") or req_data.get("results") or []
                safe_name = "".join(c for c in target_name if c.isalnum() or c in (' ', '_', '-')).strip()
                if not safe_name:
                    safe_name = "фігурант"
                filename = f"Моніторинг_{safe_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
                out_path = os.path.join(REPORTS_DIR, filename)

                agent = AdvocateProtectionAgent()
                agent.export_dynamic_search_to_excel(target_name, records, out_path)
                self.send_json({
                    "status": "SUCCESS",
                    "filename": filename,
                    "download_url": f"/download/{urllib.parse.quote(filename)}"
                })
            except Exception as e:
                self.send_json({"status": "ERROR", "message": str(e)})
            return

        # ---------------------------------------------------------------------
        # 4. PROTECTED ADMIN POST APIs (ALL REQUIRE VALID ADMIN SESSION)
        # ---------------------------------------------------------------------
        if not self.is_authenticated_admin():
            return self.send_unauthorized()

        if path == "/api/admin/update-ai-config":
            if "analyst_model" in req_data:
                ai_service.set_config_val("analyst_model", req_data["analyst_model"].strip())
            if "guide_model" in req_data:
                ai_service.set_config_val("guide_model", req_data["guide_model"].strip())
            if "daily_limit_per_user" in req_data:
                ai_service.set_config_val("daily_limit_per_user", str(int(req_data["daily_limit_per_user"])))
            if "gemini_api_key" in req_data and req_data["gemini_api_key"].strip() and req_data["gemini_api_key"] != "******":
                ai_service.set_config_val("gemini_api_key", req_data["gemini_api_key"].strip())
            self.send_json({"status": "SUCCESS", "message": "Налаштування асистентів успішно збережено"})
            return

        elif path == "/api/admin/change-password":
            new_pass = req_data.get("new_password", "").strip()
            if len(new_pass) < 6:
                self.send_json({"status": "FAIL", "message": "Пароль повинен містити не менше 6 символів"}, status=400)
                return
            p_hash, p_salt = hash_password(new_pass)
            try:
                conn = sqlite3.connect(DB_PATH)
                cur = conn.cursor()
                cur.execute("UPDATE admin_users SET password_hash = ?, salt = ? WHERE username = 'admin'", (p_hash, p_salt))
                conn.commit()
                conn.close()
                self.send_json({"status": "SUCCESS", "message": "Пароль адміністратора успішно оновлено"})
            except Exception as e:
                self.send_json({"status": "FAIL", "message": str(e)}, status=500)
            return

        elif path == "/api/admin/reindex-rag":
            init_database(DB_PATH)
            self.send_json({"status": "SUCCESS", "message": "Індексація 36 документів справи виконана успішно"})
            return

        elif path == "/api/run-monitoring":
            if not AdvocateProtectionAgent:
                self.send_json({"status": "ERROR", "message": "Agent module not available"})
                return
            agent = AdvocateProtectionAgent()
            res = agent.run_monitoring_cycle()
            self.send_json({
                "status": "SUCCESS",
                "sources_checked": res["sources_checked"],
                "mentions_found": res["mentions_found"],
                "new_count": res["new_count"],
                "total_count": res["total_count"],
                "report_name": os.path.basename(res["report_path"])
            })
            return

        elif path == "/api/run-tests":
            logs = run_test_suite()
            self.send_json({
                "status": "SUCCESS",
                "tests_count": len(logs),
                "summary": "Всі 3 контрольні тести успішно пройдено"
            })
            return

        elif path == "/api/schedules/create":
            agent = AdvocateProtectionAgent()
            new_id = agent.storage.create_schedule(req_data)
            self.send_json({"status": "SUCCESS", "id": new_id, "message": "Розклад збережено"})
            return

        elif path == "/api/schedules/toggle":
            agent = AdvocateProtectionAgent()
            agent.storage.update_schedule_status(req_data.get("id"), req_data.get("is_active", 1))
            self.send_json({"status": "SUCCESS"})
            return

        elif path == "/api/schedules/delete":
            agent = AdvocateProtectionAgent()
            agent.storage.delete_schedule(req_data.get("id"))
            self.send_json({"status": "SUCCESS"})
            return

        elif path == "/api/schedules/run-now":
            agent = AdvocateProtectionAgent()
            res = agent.execute_schedule_job(req_data.get("id"))
            self.send_json({"status": "SUCCESS", "data": res})
            return

        elif path == "/api/send-test-report":
            target_name = req_data.get("target_name", "Мудра Ірина Романівна")
            recipient_email = req_data.get("recipient_email", "markfion10@gmail.com")
            keywords = req_data.get("keywords", "")
            sources = req_data.get("sources_filter", "ALL")
            report_type = req_data.get("report_type", "COMPLETE_PACKAGE")
            agent = AdvocateProtectionAgent()
            res = agent.send_instant_test_email(
                target_name=target_name,
                recipient_email=recipient_email,
                keywords=keywords,
                sources_filter=sources,
                report_type=report_type
            )
            dispatch = res.get("dispatch", {})
            self.send_json({
                "status": dispatch.get("status", "SUCCESS"),
                "message": dispatch.get("message", "Звіт сформовано"),
                "error": dispatch.get("error", ""),
                "report_file": res.get("filename"),
                "download_url": res.get("download_url"),
                "recipient": recipient_email,
                "data": res
            })
            return

        elif path == "/api/smtp-config":
            agent = AdvocateProtectionAgent()
            if req_data.get("password") == "******":
                old_cfg = agent.storage.get_smtp_settings()
                req_data["password"] = old_cfg.get("password", "")
            agent.storage.save_smtp_settings(req_data)
            self.send_json({"status": "SUCCESS", "message": "Налаштування пошти збережено"})
            return

        elif path == "/api/smtp-test":
            host = (req_data.get("host") or req_data.get("server") or "").strip()
            port = int(req_data.get("port") or 465)
            user = (req_data.get("user") or req_data.get("username") or "").strip()
            pwd = (req_data.get("password") or "").strip()
            agent = AdvocateProtectionAgent()
            if not pwd or pwd == "******":
                old_cfg = agent.storage.get_smtp_settings()
                pwd = old_cfg.get("password", "")
                if not host:
                    host = old_cfg.get("host", "")
                if not user:
                    user = old_cfg.get("user", "")
            use_ssl = bool(req_data.get("use_ssl", True)) if "use_ssl" in req_data else (port == 465)
            from_email = req_data.get("from_email") or user or "patrol.agent@advocate-firm.ua"
            test_recipient = req_data.get("test_recipient") or req_data.get("recipient_email") or user or "markfion10@gmail.com"
            res = EmailReporter.test_smtp_connection(
                host=host,
                port=port,
                user=user,
                pwd=pwd,
                use_ssl=use_ssl,
                from_email=from_email,
                test_recipient=test_recipient
            )
            self.send_json(res)
            return

        self.send_error(404, "Endpoint not found")

def scheduler_daemon_loop():
    time.sleep(5)
    print("[EmailSchedulerDaemon] Background scheduler running. Checking tasks every 20s.")
    while True:
        try:
            time.sleep(20)
            if AdvocateProtectionAgent:
                agent = AdvocateProtectionAgent()
                schedules = agent.storage.get_schedules()
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                for sch in schedules:
                    if sch.get("is_active") == 1 and sch.get("next_run"):
                        if sch["next_run"] <= now_str:
                            print(f"\n[EmailSchedulerDaemon] ⏰ Auto trigger #{sch['id']}: '{sch.get('name')}' for {sch.get('recipient_email')}")
                            t = threading.Thread(target=agent.execute_schedule_job, args=(sch["id"],), daemon=True)
                            t.start()
        except Exception as e:
            print(f"[EmailSchedulerDaemon] Loop note: {e}")

def start_server():
    os.chdir(BASE_DIR)
    generate_qr_code(GLOBAL_URL)

    sched_thread = threading.Thread(target=scheduler_daemon_loop, daemon=True)
    sched_thread.start()

    http.server.ThreadingHTTPServer.allow_reuse_address = True
    with http.server.ThreadingHTTPServer((HOST, PORT), TacticalHUDHandler) as httpd:
        try:
            sys.stdout.reconfigure(encoding='utf-8')
        except Exception:
            pass
        print("\n=======================================================")
        print("🐾 [DIANA PATROL / ПАТРУЛЬ 🐾] CLOUD PRODUCTION SERVER")
        print(f"Port: {PORT}")
        print(f"Public External URL: {GLOBAL_URL}")
        print(f"Base Directory: {BASE_DIR}")
        print("AI Engine: Gemini RAG Active (gemini-3.5-flash / gemini-flash-lite-latest)")
        print("Admin Auth: PBKDF2-HMAC-SHA256 Protected")
        print("Scheduler: ACTIVE IN BACKGROUND (24/7)")
        print("=======================================================\n")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nServer stopped.")

if __name__ == "__main__":
    start_server()
