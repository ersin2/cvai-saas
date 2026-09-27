# Browser checks

`run.js` opens every page in Chrome at desktop (1440px) and phone (390px)
width. It fails the run if any page has:

- text below WCAG contrast (4.5:1, or 3:1 for large text);
- sideways scrolling;
- an icon that draws nothing;
- a JavaScript error or a failed request.

It also runs the flows that exist only in a browser:

- a Tools answer and a cover letter stream in progressively, while the text
  typed into the other tools stays put;
- a 429 shows a warning toast;
- a member cannot open the staff page;
- deleting an account asks first, Cancel keeps the account, and Confirm
  removes it.

CI runs this as the `browser` job in `.github/workflows/tests.yml`. Screenshots
are uploaded only when a check fails.

## Running it locally

Use a throwaway database; the seed refuses anything that is not local.

```bash
export DATABASE_URL=sqlite:///e2e.sqlite3 DEBUG=True USE_S3=False
export ANTHROPIC_API_KEY=fake ANTHROPIC_BASE_URL=http://127.0.0.1:8799
python manage.py migrate && python e2e/seed.py
python e2e/fake_anthropic.py 8799 &
python -m uvicorn aigen.asgi:application --port 8765 &
npm ci --prefix e2e && node e2e/run.js
```

Run the app under uvicorn, not `runserver`. `runserver` is WSGI, which
collects a stream before sending it, so the streaming checks would fail.
Set `CHROME_PATH` if Chrome is not in its default location. Screenshots and
`report.txt` are written to `e2e/out/`.

`e2e/fake_anthropic.py` stands in for the Anthropic API and streams canned
answers. The app reaches it through the real SDK, so everything except the
model itself is tested.
