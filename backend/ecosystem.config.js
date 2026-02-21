module.exports = {
    apps: [
        {
            name: "orbit-backend",
            script: "uvicorn",
            args: "backend.main:app --host 0.0.0.0 --port 8000 --workers 1",
            cwd: "/Users/apple/Documents/boardy_ai_new",
            interpreter: "none",
            autorestart: true,
            watch: false,
            max_memory_restart: "2G",
            env: {
                NODE_ENV: "production",
            },
            error_file: "backend/logs/backend-error.log",
            out_file: "backend/logs/backend-out.log",
        }
    ]
};