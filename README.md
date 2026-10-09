# Student Drive Bot

Telegram bot to browse and download files from a folder.

## Setup
1. Put your files in the `drive/` folder (or set `DRIVE_FOLDER` to your folder's name).
2. Get a token from @BotFather.

## Run locally
```
pip install -r requirements.txt
export BOT_TOKEN=your_token      # Windows PowerShell: $env:BOT_TOKEN="your_token"
python bot.py
```

## Environment variables
| Name | Required | Description |
|------|----------|-------------|
| `BOT_TOKEN` | yes | Token from @BotFather |
| `ALLOWED_USER_IDS` | no | Comma-separated Telegram user IDs. Empty = everyone |
| `DRIVE_FOLDER` | no | Folder with the files (default `drive`) |

## Deploy on Railway
1. Push this repo to GitHub.
2. Railway: New Project > Deploy from GitHub repo.
3. Add the variables above in the Variables tab.
4. Deploy. No port or domain needed, the bot uses polling.

To add or change files, commit them to the repo and push; Railway redeploys automatically.
