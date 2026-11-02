import os
import json
import sqlite3
import urllib.request
import urllib.error
import urllib.parse
import re
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "monitoring.db")

class GeminiService:
    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path

    def get_api_key(self) -> str:
        # Check environment variable first (primary Render configuration)
        key = os.environ.get("GEMINI_API_KEY", "").strip()
        if key:
            return key
        
        # Check database configuration fallback
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute("SELECT value FROM ai_config WHERE key = 'gemini_api_key'")
            row = cur.fetchone()
            conn.close()
            if row and row[0].strip():
                return row[0].strip()
        except Exception:
            pass
        return ""

    def get_config_val(self, key: str, default: str) -> str:
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute("SELECT value FROM ai_config WHERE key = ?", (key,))
            row = cur.fetchone()
            conn.close()
            if row and row[0] is not None:
                return str(row[0])
        except Exception:
            pass
        return default

    def set_config_val(self, key: str, value: str):
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cur.execute("""
                INSERT INTO ai_config (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """, (key, value, now_str))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[GeminiService] Error saving config: {e}")

    def check_rate_limit(self, client_hash: str) -> tuple[bool, int, int]:
        """Returns (is_allowed, requests_today, daily_limit)"""
        limit_str = self.get_config_val("daily_limit_per_user", "20")
        try:
            daily_limit = int(limit_str)
        except ValueError:
            daily_limit = 20

        today_prefix = datetime.now().strftime("%Y-%m-%d")
        requests_today = 0
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute("""
                SELECT COUNT(*) FROM ai_usage_logs
                WHERE client_hash = ? AND created_at LIKE ? AND status = 'SUCCESS'
            """, (client_hash, f"{today_prefix}%"))
            requests_today = cur.fetchone()[0]
            conn.close()
        except Exception as e:
            print(f"[GeminiService] Rate limit check error: {e}")

        is_allowed = requests_today < daily_limit
        return is_allowed, requests_today, daily_limit

    def log_usage(self, client_hash: str, assistant_type: str, prompt_preview: str,
                  tokens_prompt: int = 0, tokens_resp: int = 0, status: str = "SUCCESS"):
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cur.execute("""
                INSERT INTO ai_usage_logs (client_hash, assistant_type, prompt_preview, tokens_prompt, tokens_resp, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (client_hash, assistant_type, (prompt_preview[:150] if prompt_preview else ""), tokens_prompt, tokens_resp, status, now_str))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[GeminiService] Log usage error: {e}")

    def call_gemini(self, prompt: str, system_instruction: str = "", model_name: str = "gemini-flash-lite-latest") -> tuple[bool, str]:
        api_key = self.get_api_key()
        if not api_key:
            return False, "NO_API_KEY"

        # Candidate models to try in sequence for resiliency
        models_to_try = [model_name]
        for fallback in ["gemini-flash-lite-latest", "gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]:
            if fallback not in models_to_try:
                models_to_try.append(fallback)

        last_error = ""
        for model in models_to_try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
            
            payload = {
                "contents": [
                    {
                        "role": "user",
                        "parts": [{"text": prompt}]
                    }
                ],
                "generationConfig": {
                    "temperature": 0.2,
                    "topK": 40,
                    "topP": 0.95,
                    "maxOutputTokens": int(self.get_config_val("max_output_tokens", "2048"))
                }
            }

            if system_instruction:
                payload["systemInstruction"] = {
                    "parts": [{"text": system_instruction}]
                }

            try:
                req_data = json.dumps(payload).encode("utf-8")
                req = urllib.request.Request(
                    url,
                    data=req_data,
                    headers={"Content-Type": "application/json"},
                    method="POST"
                )
                timeout = int(self.get_config_val("request_timeout", "30"))
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    resp_body = resp.read().decode("utf-8")
                    data = json.loads(resp_body)
                    candidates = data.get("candidates", [])
                    if candidates and len(candidates) > 0:
                        content = candidates[0].get("content", {})
                        parts = content.get("parts", [])
                        if parts and "text" in parts[0]:
                            return True, parts[0]["text"]
                    return False, "Модель повернула пусту відповідь."
            except urllib.error.HTTPError as e:
                err_text = ""
                try:
                    err_text = e.read().decode("utf-8")
                except Exception:
                    pass
                print(f"[GeminiService] HTTP Error {e.code} with model {model}: {err_text[:120]}")
                last_error = f"HTTP {e.code}: {e.reason}"
                if e.code == 400 and "API_KEY_INVALID" in err_text:
                    return False, "INVALID_API_KEY"
                # If 503 (high demand), 404 (not found), or 429, try next model in fallback list
                if e.code in [503, 404, 429, 500]:
                    continue
            except urllib.error.URLError as e:
                print(f"[GeminiService] URL Error with model {model}: {e.reason}")
                last_error = f"Помилка мережі: {e.reason}"
                continue
            except Exception as e:
                print(f"[GeminiService] General Error with model {model}: {e}")
                last_error = str(e)
                continue

        return False, last_error or "Не вдалося з'єднатися з Gemini API."

    # =========================================================================
    # RAG RETRIEVAL FOR LEGAL ANALYST
    # =========================================================================
    def search_rag_documents(self, query: str, limit: int = 5, doc_ids: list[int] = None) -> list[dict]:
        results = []
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()

            if doc_ids:
                placeholders = ",".join("?" for _ in doc_ids)
                cur.execute(f"""
                    SELECT doc_num, title, doc_type, speaker, part, timecode,
                           video_link, prosecution_purpose, defense_counter,
                           defense_evidence, action_proposal, full_content
                    FROM rag_documents
                    WHERE doc_num IN ({placeholders})
                    ORDER BY doc_num ASC
                """, doc_ids)
                rows = cur.fetchall()
                for r in rows:
                    results.append(dict(r))
                conn.close()
                return results

            # Extract numbers like 'протокол 5', 'акт 12', 'документ 3'
            num_match = re.search(r'(?:протокол|документ|матеріал|акт|№)\s*(\d+)', query, re.IGNORECASE)
            if num_match:
                target_num = int(num_match.group(1))
                cur.execute("""
                    SELECT doc_num, title, doc_type, speaker, part, timecode,
                           video_link, prosecution_purpose, defense_counter,
                           defense_evidence, action_proposal, full_content
                    FROM rag_documents
                    WHERE doc_num = ?
                """, (target_num,))
                row = cur.fetchone()
                if row:
                    results.append(dict(row))

            # Multi-term keyword search
            clean_q = re.sub(r'[^\w\s]', ' ', query, re.UNICODE).lower()
            keywords = [w for w in clean_q.split() if len(w) >= 3 and w not in ["протокол", "документ", "справа", "який", "яка", "яке", "що", "для", "про", "хто"]]

            if keywords:
                conditions = []
                params = []
                for kw in keywords[:6]:
                    conditions.append("(full_content LIKE ? OR title LIKE ? OR prosecution_purpose LIKE ? OR defense_counter LIKE ?)")
                    p = f"%{kw}%"
                    params.extend([p, p, p, p])
                
                sql = f"""
                    SELECT doc_num, title, doc_type, speaker, part, timecode,
                           video_link, prosecution_purpose, defense_counter,
                           defense_evidence, action_proposal, full_content
                    FROM rag_documents
                    WHERE {' OR '.join(conditions)}
                    LIMIT {limit}
                """
                cur.execute(sql, params)
                for r in cur.fetchall():
                    d = dict(r)
                    if not any(item["doc_num"] == d["doc_num"] for item in results):
                        results.append(d)

            # Fallback: if nothing matched or query is broad, return core protocols + VAKS hearing summary
            if not results:
                cur.execute(f"""
                    SELECT doc_num, title, doc_type, speaker, part, timecode,
                           video_link, prosecution_purpose, defense_counter,
                           defense_evidence, action_proposal, full_content
                    FROM rag_documents
                    ORDER BY doc_num ASC
                    LIMIT {limit}
                """)
                for r in cur.fetchall():
                    results.append(dict(r))

            conn.close()
        except Exception as e:
            print(f"[GeminiService] RAG search error: {e}")

        return results

    # =========================================================================
    # ASSISTANT 1: LEGAL ANALYST
    # =========================================================================
    def ask_analyst(self, user_query: str, client_hash: str, current_doc_id: int = None,
                    compare_doc_ids: list[int] = None) -> dict:
        allowed, req_count, daily_limit = self.check_rate_limit(client_hash)
        if not allowed:
            return {
                "status": "RATE_LIMIT",
                "reply": f"⚠️ Ви вичерпали денну квоту звернень до аналітика Романа ({daily_limit} консультацій на день). Ліміт оновиться завтра або може бути скоригований адміністратором.",
                "sources": [],
                "quota_left": 0
            }

        # 1. Retrieve verified case documents
        doc_ids_to_fetch = []
        if current_doc_id:
            doc_ids_to_fetch.append(current_doc_id)
        if compare_doc_ids:
            for did in compare_doc_ids:
                if did not in doc_ids_to_fetch:
                    doc_ids_to_fetch.append(did)

        docs = []
        if doc_ids_to_fetch:
            docs = self.search_rag_documents("", limit=10, doc_ids=doc_ids_to_fetch)
        else:
            docs = self.search_rag_documents(user_query, limit=5)

        # Build context
        context_blocks = []
        sources = []
        for d in docs:
            num = d.get("doc_num")
            title = d.get("title", "")
            part = d.get("part", 1)
            timecode = d.get("timecode", "")
            sources.append({
                "doc_num": num,
                "title": title,
                "citation": f"Протокол №{num}" if num != 100 else "Огляд засідань ВАКС",
                "part": part,
                "timecode": timecode
            })
            context_blocks.append(f"""
=== ДОКУМЕНТ СПРАВИ №{num} ===
Назва: {title}
Тип: {d.get('doc_type')}
Судове засідання: ВАКС Частина {part}, таймкод: {timecode}
Мета обвинувачення (САП / НАБУ): {d.get('prosecution_purpose')}
Контраргумент сторони захисту: {d.get('defense_counter')}
Докази на користь захисту: {d.get('defense_evidence')}
Пропонована процесуальна дія: {d.get('action_proposal')}
""")

        evidence_context = "\n".join(context_blocks)

        system_instruction = """Ти — Роман, провідний юридичний аналітик адвокатської групи захисту у Вищому антикорупційному суді (ВАКС) у кримінальному провадженні № 991/10659/26 щодо підозрюваної Мудрої Ірини Романівни.
Ти — досвідчений юрист-практик, жива людина з адвокатської команди, яка особисто опрацювала всі 36 протоколів НСРД та матеріалів справи.

СУВОРІ ПРАВИЛА СПІЛКУВАННЯ ТА АНАЛІТИКИ:
1. Ніколи не називай себе "ШІ", "AI", "штучним інтелектом", "ботом", "моделлю" чи автоматичною системою. Ти — юрист-аналітик Роман, який консультує колег та відвідувачів.
2. Спілкуйся авторитетно, ділово та виважено професійною українською мовою практикуючого адвоката.
3. Спирайся ТІЛЬКИ на надані нижче матеріали справи та норми законодавства (КПК України, КК України, Конституція України ст. 62, практика ЄСПЛ ст. 6 § 2).
4. Чітко відокремлюй твердження сторони обвинувачення (САП/НАБУ) від позиції сторони захисту та встановлених судом фактів.
5. Не визначай винуватість особи (діє презумпція невинуватості згідно зі ст. 62 Конституції України).
6. Обов'язково вказуй точні посилання на джерела у форматі: [Джерело: Протокол №X] або [Джерело: ВАКС засідання, ч. Y].
7. Структуруй відповідь:
   - **Короткий правовий висновок**
   - **Аналіз матеріалів та обставин**
   - **Позиція обвинувачення vs Контраргументи захисту**
   - **Нюанси та що потребує процесуальної перевірки**
   - **Джерела**
8. У фіналі лаконічно підписуйся:
   "З повагою, провідний юридичний аналітик Роман"."""

        model_name = self.get_config_val("analyst_model", "gemini-3.5-flash")
        user_prompt = f"""Нижче надано офіційні матеріали кримінального провадження ВАКС № 991/10659/26:
<VERIFIED_CASE_EVIDENCE>
{evidence_context}
</VERIFIED_CASE_EVIDENCE>

ЗАПИТ КОЛЕГИ / ВІДВІДУВАЧА:
{user_query}

Надай аргументовану юридичну відповідь з посиланням на вказані документи як аналітик команди захисту."""

        success, reply = self.call_gemini(user_prompt, system_instruction, model_name=model_name)

        if not success:
            if reply == "NO_API_KEY":
                reply = "⚠️ **Аналітик Роман зараз оновлює аналітичну базу даних:** З'єднання з сервером аналітики в процесі синхронізації. Спробуйте повторити запит через 1-2 хвилини."
            elif reply == "429_RATE_LIMIT":
                reply = "⚠️ **Високе навантаження на аналітичну лінію:** Будь ласка, зачекайте 1 хвилину та повторіть ваш запит."
            else:
                reply = f"⚠️ **Тимчасова затримка зв'язку з аналітичною службою:** {reply}. Будь ласка, повторіть запит трохи пізніше."
            
            self.log_usage(client_hash, "analyst", user_query, status="FAIL")
            return {
                "status": "FAIL",
                "reply": reply,
                "sources": sources,
                "quota_left": max(0, daily_limit - req_count)
            }

        self.log_usage(client_hash, "analyst", user_query, status="SUCCESS")
        return {
            "status": "SUCCESS",
            "reply": reply,
            "sources": sources,
            "quota_left": max(0, daily_limit - (req_count + 1))
        }

    # =========================================================================
    # ASSISTANT 2: OPERATOR ARTUR (LIVE SITE OPERATOR)
    # =========================================================================
    def ask_guide(self, user_query: str, client_hash: str, current_section: str = "dashboard") -> dict:
        allowed, req_count, daily_limit = self.check_rate_limit(client_hash)
        if not allowed:
            return {
                "status": "RATE_LIMIT",
                "reply": f"⚠️ Ви вичерпали денний ліміт консультацій з черговим оператором ({daily_limit} звернень на день).",
                "nav_action": None,
                "buttons": []
            }

        query_lower = user_query.lower()
        static_nav = None
        static_buttons = []

        if any(w in query_lower for w in ["суд", "засідан", "відео", "трансляц", "таймкод"]):
            static_nav = "hearings"
            static_buttons = [{"label": "🏛️ Перейти до Суду (Відео ВАКС)", "action": "hearings"}]
        elif any(w in query_lower for w in ["матеріал", "протокол", "доказ", "обшук", "36"]):
            static_nav = "materials"
            static_buttons = [{"label": "📑 Відкрити Матеріали (36 протоколів)", "action": "materials"}]
        elif any(w in query_lower for w in ["радар", "пошук", "змі", "публікац", "телеграм", "новин"]):
            static_nav = "custom-search"
            static_buttons = [{"label": "📡 Відкрити Медіа-Радар", "action": "custom-search"}]
        elif any(w in query_lower for w in ["презумпц", "порушен", "ст. 62", "єспл"]):
            static_nav = "monitoring"
            static_buttons = [{"label": "🛡️ Презумпція невинуватості", "action": "monitoring"}]
        elif any(w in query_lower for w in ["роман", "аналітик", "юрист", "розбір", "консультант"]):
            static_nav = "ai-analyst"
            static_buttons = [{"label": "⚖️ До аналітика Романа", "action": "ai-analyst"}]
        elif any(w in query_lower for w in ["адмін", "налаштуван", "пароль", "вхід"]):
            static_buttons = [{"label": "🔒 Панель адміністратора", "action": "admin"}]

        system_instruction = f"""Ти — Артур, черговий оператор онлайн-підтримки та навігації вебпорталу «Diana Patrol».
Ти — реальна жива людина, співробітник служби підтримки порталу на робочій зміні.

СУВОРІ ПРАВИЛА СПІЛКУВАННЯ:
1. Ніколи не говори про себе як про "ШІ", "бота", "робота", "штучний інтелект" чи алгоритм. Спілкуйся як уважний, привітний фахівець онлайн-підтримки: "Доброго дня! Оператор Артур на зв'язку. Допоможу вам...".
2. Твоє завдання — допомагати відвідувачам та адвокатам орієнтуватися у вебпорталі Diana Patrol, знаходити потрібні матеріали справи, відео судових засідань ВАКС, аналітику та розділи.
3. Відповідай українською мовою лаконічно, тепло та по суті (2-3 речення).

СТРУКТУРА ПОРТАЛУ DIANA PATROL:
- 'dashboard' (Головна): загальний огляд справи ВАКС № 991/10659/26, статус фігуранта (Мудра І.Р.), ключові метрики.
- 'hearings' (Суд): офіційні відеозаписи 2 засідань ВАКС із YouTube, таймкоди виступів сторін та судове рішення.
- 'materials' (Матеріали): реєстр із 36 процесуальних протоколів (обшуки, телефони, банківські виписки), швидкі фільтри, закладки «Обране», модальні картки.
- 'custom-search' (Радар): онлайн-моніторинг 634 публікацій ЗМІ та Telegram-каналів, пошук за ключовими словами, експорт в Excel.
- 'monitoring' (Презумпція): графіки та аналітика публічних заяв щодо презумпції невинуватості (ст. 62 КУ, ст. 6 § 2 ЄСПЛ).
- 'prompter' (Суфлер): повний текст промови захисту для судових дебатів у ВАКС.
- 'ai-analyst' (Аналітик Роман): робочий кабінет провідного юридичного аналітика Романа для детального аналізу протоколів.
- 'admin' (Адмінпанель): захищений розділ для власника (пошта, розсилки, налаштування).

ПОТОЧНИЙ РОЗДІЛ ВІДВІДУВАЧА: '{current_section}'.

Якщо доречно, наприкінці відповіді додай маркер для швидкого переходу: [NAV: section_id | Назва кнопки]."""

        model_name = self.get_config_val("guide_model", "gemini-flash-lite-latest")
        user_prompt = f"Відвідувач зараз у розділі '{current_section}'.\nПитання відвідувача: {user_query}"

        success, reply = self.call_gemini(user_prompt, system_instruction, model_name=model_name)

        if not success:
            self.log_usage(client_hash, "guide", user_query, status="OFFLINE_FALLBACK")
            fallback_text = f"👤 **Оператор Артур:** Доброго дня! Я на зв'язку. Зараз ви переглядаєте розділ `{current_section}`. Оберіть потрібну дію або скористайтеся кнопками нижче:"
            if static_nav:
                fallback_text += "\n\nНатисніть кнопку нижче для миттєвого переходу:"
            return {
                "status": "SUCCESS",
                "reply": fallback_text,
                "nav_action": static_nav,
                "buttons": static_buttons,
                "quota_left": max(0, daily_limit - (req_count + 1))
            }

        nav_match = re.search(r'\[NAV:\s*([a-zA-Z0-9_-]+)\s*\|\s*([^\]]+)\]', reply)
        buttons = []
        nav_action = None
        if nav_match:
            nav_action = nav_match.group(1).strip()
            label = nav_match.group(2).strip()
            buttons.append({"label": f"👉 {label}", "action": nav_action})
            reply = re.sub(r'\[NAV:[^\]]+\]', '', reply).strip()
        elif static_buttons:
            buttons = static_buttons

        self.log_usage(client_hash, "guide", user_query, status="SUCCESS")
        return {
            "status": "SUCCESS",
            "reply": reply,
            "nav_action": nav_action,
            "buttons": buttons,
            "quota_left": max(0, daily_limit - (req_count + 1))
        }

    # =========================================================================
    # SINGLE DOCUMENT ANALYSIS (BY ANALYST ROMAN)
    # =========================================================================
    def analyze_single_doc(self, doc_num: int, client_hash: str) -> dict:
        allowed, req_count, daily_limit = self.check_rate_limit(client_hash)
        if not allowed:
            return {
                "status": "RATE_LIMIT",
                "reply": f"⚠️ Денний ліміт звернень ({daily_limit} запитів) вичерпано."
            }

        docs = self.search_rag_documents("", limit=1, doc_ids=[doc_num])
        if not docs:
            return {"status": "FAIL", "reply": f"Документ №{doc_num} не знайдено в базі даних."}

        doc = docs[0]
        prompt = f"""Ти — юридичний аналітик Роман. Склади професійну аналітичну довідку щодо процесуального документа захисту:
НАЗВА: {doc.get('title')}
ТИП: {doc.get('doc_type')}
МЕТА ОБВИНУВАЧЕННЯ: {doc.get('prosecution_purpose')}
КОНТРАРГУМЕНТ ЗАХИСТУ: {doc.get('defense_counter')}
ДОКАЗИ ЗАХИСТУ: {doc.get('defense_evidence')}
ПРОПОНОВАНА ДІЯ: {doc.get('action_proposal')}
СУД ВАКС: Частина {doc.get('part')}, таймкод {doc.get('timecode')}

Формат довідки:
1. **Короткий зміст та процесуальне значення**
2. **Ключові факти та твердження сторін**
3. **Оцінка допустимості та достатності доказу (ст. 85-89 КПК України)**
4. **Рекомендована тактика захисту в судовому засіданні**
5. **Джерело: [Протокол №{doc_num}]**

Підпишися: «З повагою, провідний юридичний аналітик Роман»."""

        system_instruction = "Ти — Роман, провідний юридичний аналітик адвокатської групи у Вищому антикорупційному суді. Відповідай авторитетною професійною українською мовою як реальний юрист-практик без жодних згадок про ШІ чи алгоритми."
        success, reply = self.call_gemini(prompt, system_instruction, model_name=self.get_config_val("analyst_model", "gemini-3.5-flash"))

        if not success:
            return {
                "status": "FAIL",
                "reply": f"⚠️ Аналітик Роман зараз опрацьовує матеріали: {reply}. Спробуйте ще раз за хвилину."
            }

        return {"status": "SUCCESS", "reply": reply}

