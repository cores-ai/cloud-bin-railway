# Railway bot

Telegram intake for the folder archive. It only receives. There are no commands.

An owner can send the two photos and the email in any order. A photo is a picture message, a forwarded picture, or an image file. An email is any text that contains `@` and a domain, Gmail or otherwise. When both photos and the email are in, the bot stores them in Cloudflare D1 with the current India time.

After the first photo the bot says `Send another pic`. After both photos it asks for the email. If the email arrives first, it asks for the pictures.

## Env

Use the same values as the Vercel app:

- `CLOUDFLARE_ACCOUNT_ID`
- `CLOUDFLARE_API_TOKEN`
- `CLOUDFLARE_D1_DATABASE_ID`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_OWNER_IDS` = `8344952568,7562427494`

## Deploy

1. Push this folder as its own repo.
2. On Railway, create a service from that repo.
3. Paste the env vars.
4. Start command is `python bot.py`.

The process also answers `GET /` with `ok` on Railway's `PORT`, so a web service stays up while the bot long-polls.
