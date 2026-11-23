# ClipSafe AI on your Mac (free, no card)

This runs the whole app on your own Mac using free services only:

| Job | Free tool |
|-----|-----------|
| Pick the best moments, write commentary | Gemini API, free Google AI Studio key |
| Voiceover | edge-tts (Malayalam, Hindi, Tamil, English voices) |
| Captions | Whisper (runs on your Mac) |
| Speaker tracking for 9:16 | OpenCV face detector (runs on your Mac) |
| B-roll | Pexels free stock photos (optional free key), otherwise blurred frames from the clip |
| Vocal isolation, rendering | demucs + FFmpeg (run on your Mac) |

Use it only on videos you own or have permission to clip.

## Steps (മലയാളം)

1. **Gemini key എടുക്കുക:** https://aistudio.google.com/apikey → Sign in → *Create API key* → copy.
2. **(Optional) Pexels key:** https://www.pexels.com/api/ → sign up → *Your API Key* → copy.
3. **Code download ചെയ്യുക:** GitHub repo → green **Code** button → **Download ZIP** → Downloads-ൽ unzip ചെയ്യുക.
4. **Terminal തുറക്കുക:** `Cmd + Space` → "Terminal" → Enter.
5. **Install:** Terminal-ൽ `bash ` എന്ന് type ചെയ്യുക (bash-ന് ശേഷം ഒരു space), എന്നിട്ട് unzip ചെയ്ത folder-ലെ `local/install-mac.sh` file Terminal window-ലേക്ക് drag ചെയ്ത് ഇടുക, Enter അമർത്തുക.
   - Mac password ചോദിച്ചാൽ type ചെയ്യുക (type ചെയ്യുമ്പോൾ ഒന്നും കാണില്ല, അത് സാധാരണമാണ്).
   - Gemini key ചോദിക്കുമ്പോൾ paste ചെയ്ത് Enter.
   - ആദ്യ install-ന് 15–30 മിനിറ്റ് എടുക്കാം.
6. **Start:** Dock-ലെ **ClipSafe AI** icon click ചെയ്യുക (Applications folder-ലും ഉണ്ട്). ഏകദേശം 20 സെക്കൻഡിനുള്ളിൽ browser-ൽ http://localhost:3000 തുറക്കും. Terminal വേണ്ട. നിർത്താൻ Applications-ലെ **Stop ClipSafe** click ചെയ്യുക. ("Documents folder access" ചോദിച്ചാൽ **Allow**.)
7. YouTube link ഇടുക, "I own this video" tick ചെയ്യുക, **Generate**.

**Update ചെയ്യാൻ:** Terminal-ൽ ClipSafe folder-ലേക്ക് `cd` ചെയ്ത് ഇത് run ചെയ്യുക:

```bash
curl -fsSL https://github.com/naseemjavadk-creator/videoai/archive/refs/heads/main.tar.gz | tar -xz --strip-components=1 && bash local/update-mac.sh
```


## Notes

- **Speed:** Gemini watches the YouTube link directly, and only the chosen clip sections are
  downloaded (720p). On Apple Silicon, vocal isolation uses the GPU and rendering uses the
  hardware video encoder. Expect roughly 2–3 minutes per Short. Set `DOWNLOAD_HEIGHT=1080` in
  `backend/.env` for sharper (slower) downloads.
- **Free limits:** the Gemini free tier has daily limits. ClipSafe uses Gemini 3.8 Flash and falls back to
  Gemini 3.5 Flash-Lite when the free daily quota runs out. If Google retires a model, ClipSafe
  follows the replacement Google names; you can also set `GEMINI_MODEL` in `backend/.env`. Free-tier prompts may be used by Google
  to improve its products.
- **Captions quality:** for better Malayalam captions set `WHISPER_MODEL=medium` in
  `backend/.env` and run the installer again (slower, uses more memory).
- **Keys stay on your Mac:** they are saved in `backend/.env` and `frontend/.env.local`, which are
  never uploaded to GitHub.
- **Storage:** finished Shorts are in the `data/` folder and are deleted after 24 hours.
- **YouTube upload (optional):** create an OAuth "Web application" client in Google Cloud Console
  (free, no billing needed), enable *YouTube Data API v3*, add the redirect URI
  `http://localhost:3000/api/youtube/callback`, then put `GOOGLE_OAUTH_CLIENT_ID` and
  `GOOGLE_OAUTH_CLIENT_SECRET` in `frontend/.env.local` and restart. Without it, use **Download**
  and upload in the YouTube app.
- **Logs:** `data/logs/backend.log` and `data/logs/frontend.log`.
