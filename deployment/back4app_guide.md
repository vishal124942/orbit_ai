# Deploying Orbit AI Backend to Back4App

Back4App operates on Docker containers (Back4App Containers CaaS). Because Orbit AI is a unique hybrid architecture (Python FastAPI spawns a detached Node.js Baileys instance), deploying it requires exactly this setup.

The `backend/Dockerfile` has been explicitly designed for Back4App!

## Step 1: Push your repo to GitHub
Ensure all your recent changes are pushed to your GitHub repository.
**Important:** Your `.env` and `agent.db` files are in your `.gitignore` and will *not* be pushed. This is correct! Never push keys.

## Step 2: Create a Back4App Container App
1. Log in to [Back4App Containers](https://www.back4app.com/).
2. Click **Build new app** -> **Containers**.
3. Connect your GitHub account and select your Orbit repository.
4. Name your app (e.g., `orbit-backend`).

## Step 3: Configure Settings
In the Back4App settings screen before clicking deploy:

1. **Root Directory:** Set this to `.` (the main repository root where it can access both `backend/` and `package.json` if needed).
2. **Auto-Deploy:** Optional, but handy!
3. **Environmental Variables (CRITICAL):**
   Click "Add Environment Variable" and add every key from your local `backend/.env` file. Back4App needs these to run:
   - `OPENAI_API_KEY` = `sk-proj-...`
   - `SARVAM_API_KEY` = `your-key`
   - `PLATFORM_DB_URL` = `your-neon-postgres-url` (Mandatory for Cloud!)
   - `JWT_SECRET` = `your-secret`
   
   **Cloudflare R2 Variables (Crucial for WhatsApp Auth):**
   Because Back4App containers are *ephemeral* (they wipe local files when they restart), you **MUST** use the R2 bucket for your WhatsApp QR login so you don't get logged out every time the server sleeps.
   - `R2_BUCKET_NAME` = `your-bucket`
   - `R2_ACCOUNT_ID` = `...`
   - `R2_ACCESS_KEY_ID` = `...`
   - `R2_SECRET_ACCESS_KEY` = `...`

## Step 4: Storage (Optional but Recommended)
Back4App wipes the `data/users/` folder when the container shuts down. Your users will lose their `.md` soul files and their `agent.db` chat history.

To fix this, you must **Mount a Volume** in Back4App:
1. In the app settings on Back4App, look for **Disk Storage** or **Volumes** (only available on paid container plans).
2. Create a volume.
3. Map the volume path to: `/app/data/users`

*Note: If you don't mount a volume, the bot will still work perfectly, but it will have "amnesia" and generate a brand new Soul every time the server restarts.*

## Step 5: Deploy!
Click **Create App**. 
Back4App will recognize the `backend/Dockerfile`, build the Ubuntu image, install Python and Node.js, and launch the Uvicorn server automatically.

**Check the Logs:**
Once running, click the Logs tab in Back4App. As long as you provided the R2 variables, you will see the Python server boot up and the Node.js bridge connect to the R2 bucket!
