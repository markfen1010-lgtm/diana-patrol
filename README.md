# 🐾 Патруль 🐾 (Diana Patrol) — Cloud Production Release

Автономний вебзастосунок та AI-агент моніторингу захисту презумпції невинуватості у стилі Netflix Dark Cinema.

## 🚀 Деплой на Render.com (Безкоштовно, 24/7)

1. Створіть новий безкоштовний репозиторій на GitHub та запуште вміст цієї папки:
   ```bash
   git init
   git add .
   git commit -m "Diana Patrol UI 2.0 Production Release"
   git branch -M main
   git remote add origin https://github.com/<YOUR_USERNAME>/diana-patrol.git
   git push -u origin main
   ```
2. Перейдіть на [dashboard.render.com](https://dashboard.render.com/) -> **New +** -> **Web Service**.
3. Підключіть свій GitHub-репозиторій `diana-patrol`.
4. Render автоматично розпізнає конфігурацію з `render.yaml` або вкажіть:
   - **Environment**: Python
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python app_server.py`
   - **Plan**: Free
5. Натисніть **Deploy Web Service**.
6. Ваш застосунок отримає постійну захищену HTTPS-адресу, наприклад:
   `https://diana-patrol.onrender.com`

## 📦 Вміст збірки
- `app.html` — Оптимізований фронтенд застосунку з PWA, анімацією лапи, вкладками та темною темою.
- `app_server.py` — Автономний HTTP сервер із підтримкою динамічних портів Render, API ендпоінтів та фонового планувальника.
- `agent.py` — Логіка збору новин, правового аналізу та поштового робота.
- `monitoring.db` — База даних SQLite із 634 публікаціями, розкладами та логами.
- `materials.json` — 36 процесуальних протоколів обвинувачення САП/ВАКС.
- `manifest.webmanifest`, `icon-*.png`, `apple-touch-icon.png` — Повна конфігурація PWA для додавання на головний екран iPhone/Android.
- `render.yaml`, `Procfile`, `requirements.txt` — Файли конфігурації для автоматичного хмарного деплою.
