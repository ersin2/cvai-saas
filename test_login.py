import httpx, re, sys

client = httpx.Client(base_url='http://127.0.0.1:8000', follow_redirects=True)
resp = client.get('/login/')
csrf = resp.cookies.get('csrftoken', '')
if not csrf:
    m = re.search(r'csrfmiddlewaretoken" value="([^"]+)"', resp.text)
    if m:
        csrf = m.group(1)

r = client.post(
    '/login/',
    data={'username': 'admin', 'password': 'admin', 'csrfmiddlewaretoken': csrf},
    headers={'Referer': 'http://127.0.0.1:8000/login/'}
)
has_session = 'sessionid' in client.cookies
print(f'Login status: {r.status_code} | Has session: {has_session}')
if not has_session:
    print("Login FAILED with password 'admin'")
    sys.exit(1)
else:
    print("Login OK!")
