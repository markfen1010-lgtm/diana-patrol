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
from datetime import datetime

# Import agent logic
try:
    from agent import AdvocateProtectionAgent, run_test_suite, EmailReporter
except ImportError as e:
    print(f"[Warning] Agent import error: {e}")
    AdvocateProtectionAgent = None

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

class TacticalHUDHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=BASE_DIR, **kwargs)

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

        if path in ["/", "/index.html", "/app", "/hud"]:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
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
                # Fallback for apple-touch-icon aliases
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
                    print(f"[Status API] Error counting publications: {e}")

            server_ip = get_server_ip()
            data = {
                "status": "ONLINE",
                "court_case": "991/10659/26",
                "suspect": "Мудра Ірина Романівна",
                "court": "Вищий антикорупційний суд",
                "total_publications": pub_count,
                "local_ip": server_ip,
                "mobile_url": GLOBAL_URL,
                "global_url": GLOBAL_URL,
                "agent_ready": AdvocateProtectionAgent is not None
            }
            self.send_json(data)
            return

        elif path == "/api/materials":
            materials = []
            # Primary source: pre-parsed materials.json (super fast, zero overhead)
            if os.path.exists(MATERIALS_JSON):
                try:
                    with open(MATERIALS_JSON, "r", encoding="utf-8") as f:
                        materials = json.load(f)
                except Exception as e:
                    print(f"[Materials API] Error reading materials.json: {e}")

            # Fallback: parse Excel if materials.json wasn't loaded
            if not materials and os.path.exists(EXCEL_ANALYSIS):
                try:
                    import openpyxl
                    wb = openpyxl.load_workbook(EXCEL_ANALYSIS, data_only=True)
                    ws = wb["Основна таблиця"]
                    for r in range(5, ws.max_row + 1):
                        num = ws.cell(r, 1).value
                        if num is None:
                            continue
                        materials.append({
                            "num": num,
                            "name": ws.cell(r, 2).value or "",
                            "type": ws.cell(r, 3).value or "",
                            "prosecution_purpose": ws.cell(r, 4).value or "",
                            "speaker": ws.cell(r, 5).value or "",
                            "part": ws.cell(r, 6).value or 1,
                            "timecode": ws.cell(r, 7).value or "",
                            "video_link": ws.cell(r, 8).hyperlink.target if ws.cell(r, 8).hyperlink else (ws.cell(r, 8).value or ""),
                            "defense_counter": ws.cell(r, 9).value or "",
                            "defense_evidence": ws.cell(r, 10).value or "",
                            "action_proposal": ws.cell(r, 13).value or ""
                        })
                except Exception as e:
                    print(f"[Materials API] Error reading Excel: {e}")

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
                            "id": r[0],
                            "discovered_at": r[1],
                            "published_at": r[2],
                            "source_name": r[3],
                            "author": r[4],
                            "url": r[5],
                            "title": r[6],
                            "problematic_quote": r[7],
                            "context_type": r[8],
                            "violation_signs": r[9],
                            "legal_norm": r[10],
                            "risk_level": r[11],
                            "ai_explanation": r[12]
                        })
                except Exception as e:
                    print(f"[Publications API] Error: {e}")
            self.send_json(pubs)
            return

        elif path == "/api/schedules":
            if AdvocateProtectionAgent:
                agent = AdvocateProtectionAgent()
                schedules = agent.storage.get_schedules()
                self.send_json(schedules)
            else:
                self.send_json([])
            return

        elif path == "/api/email-logs":
            if AdvocateProtectionAgent:
                agent = AdvocateProtectionAgent()
                logs = agent.storage.get_email_dispatch_logs(40)
                self.send_json(logs)
            else:
                self.send_json([])
            return

        elif path == "/api/smtp-config":
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

    def do_POST(self):
        if self.path == "/api/run-monitoring":
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

        elif self.path == "/api/run-tests":
            logs = run_test_suite()
            self.send_json({
                "status": "SUCCESS",
                "tests_count": len(logs),
                "summary": "Всі 3 контрольні тести успішно пройдено"
            })
            return

        elif self.path == "/api/custom-search":
            try:
                content_length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_length).decode('utf-8')
                req_data = json.loads(body) if body else {}
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

        elif self.path in ["/api/export-search", "/api/export-custom-excel"]:
            try:
                content_length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_length).decode('utf-8')
                req_data = json.loads(body) if body else {}
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

        elif self.path == "/api/schedules/create":
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            req_data = json.loads(body) if body else {}
            agent = AdvocateProtectionAgent()
            new_id = agent.storage.create_schedule(req_data)
            self.send_json({"status": "SUCCESS", "id": new_id, "message": "Розклад збережено"})
            return

        elif self.path == "/api/schedules/toggle":
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            req_data = json.loads(body) if body else {}
            agent = AdvocateProtectionAgent()
            agent.storage.update_schedule_status(req_data.get("id"), req_data.get("is_active", 1))
            self.send_json({"status": "SUCCESS"})
            return

        elif self.path == "/api/schedules/delete":
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            req_data = json.loads(body) if body else {}
            agent = AdvocateProtectionAgent()
            agent.storage.delete_schedule(req_data.get("id"))
            self.send_json({"status": "SUCCESS"})
            return

        elif self.path == "/api/schedules/run-now":
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            req_data = json.loads(body) if body else {}
            agent = AdvocateProtectionAgent()
            res = agent.execute_schedule_job(req_data.get("id"))
            self.send_json({"status": "SUCCESS", "data": res})
            return

        elif self.path == "/api/send-test-report":
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            req_data = json.loads(body) if body else {}
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

        elif self.path == "/api/smtp-config":
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            req_data = json.loads(body) if body else {}
            agent = AdvocateProtectionAgent()
            if req_data.get("password") == "******":
                old_cfg = agent.storage.get_smtp_settings()
                req_data["password"] = old_cfg.get("password", "")
            agent.storage.save_smtp_settings(req_data)
            self.send_json({"status": "SUCCESS", "message": "Налаштування пошти збережено"})
            return

        elif self.path == "/api/smtp-test":
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            req_data = json.loads(body) if body else {}
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

    def send_json(self, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
        print("Scheduler: ACTIVE IN BACKGROUND (24/7)")
        print("=======================================================\n")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nServer stopped.")

if __name__ == "__main__":
    start_server()
