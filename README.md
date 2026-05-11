# Pareeksha Gurukul — Answer Submission Web App Bot

Students receive questions via Telegram, click a button to open a branded Web App,
type their answer, and submit. Evaluators rate 1–5 stars in the eval group.
Score is auto-DMed to the student.

---

## File Structure

```
app.py              — Flask backend + Telegram bot (single file)
static/
  index.html        — Pareeksha Gurukul branded Telegram Web App UI
requirements.txt
nixpacks.toml       — Pins Python 3.11 for Railway
Procfile            — gunicorn web server
db.json             — Auto-created (students, submissions)
```

---

## Railway Environment Variables

| Variable       | Value                                      |
|----------------|--------------------------------------------|
| BOT_TOKEN      | BotFather se mila token                    |
| ADMIN_ID       | Aapka Telegram user ID (number only)       |
| EVAL_GROUP_ID  | Eval group chat ID (negative number)       |
| WEBAPP_URL     | Railway app URL e.g. https://xyz.railway.app |

---

## Setup Steps

### 1. Bot + BotFather
- /newbot → get BOT_TOKEN

### 2. Allow Web App Domain (IMPORTANT)
BotFather mein:
  /setdomain → @YourBot → your-app.railway.app

### 3. Get ADMIN_ID
- @userinfobot ko message karein

### 4. Create Eval Group + Get EVAL_GROUP_ID
- Private group banayein → bot ko admin banayein
- Group mein message bhejein
- https://api.telegram.org/bot<TOKEN>/getUpdates
- "chat":{"id":-100xxxxxxx} → EVAL_GROUP_ID

### 5. Deploy on Railway
1. Files GitHub repo mein push karein
2. Railway → New Project → Deploy from GitHub
3. Env variables set karein
4. Deploy → Railway URL copy karein → WEBAPP_URL mein daalein → Redeploy

---

## Admin Commands
- /ask Sawaal yahan  → question + Web App button sabko bhejein
- /students          → list
- /reset             → naya round
- /help              → help

## Student Flow
1. /start → register
2. Admin /ask → "Jawab Dijiye" button milta hai
3. Button → Web App Telegram ke andar khulta hai
4. Answer likhein → Submit
5. Eval group mein ⭐ buttons ke saath forward
6. Rating → student ko DM mein score
