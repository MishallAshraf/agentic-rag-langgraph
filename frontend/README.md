# Bot frontend (Step 12)

A React, TypeScript and Vite app for the bots served by `../api.py`.

```bash
# terminal 1, in the project root
.\.venv\Scripts\python.exe api.py

# terminal 2, in this folder
npm install
npm run dev        # http://localhost:5173
```

In development, Vite forwards `/api/*` to the API on `127.0.0.1:8000` (see `vite.config.ts`), so the browser only talks to one origin.

| File | Contents |
|---|---|
| `src/api.ts` | Typed API client, and a Server-Sent Events reader for POST requests |
| `src/App.tsx` | Sign-in state, bot list and usage, and the open bot from the URL (`#/bot/<id>`) |
| `src/Login.tsx` | Sign-in form (accounts are created with `manage_users.py`) |
| `src/Chat.tsx` | The conversation: streaming, draft preview, steps, sources, approval card |
| `src/steps.ts` | Status events as readable steps |
| `src/Avatar.tsx` | The avatar icons a bot can choose |
| `src/index.css` | Styles. Each bot's colour enters as one variable, `--primary` |

Other commands: `npm run build` (type check and production build into `dist/`) and `npm run lint`.
