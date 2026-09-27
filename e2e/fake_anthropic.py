"""
A stand-in for the Anthropic Messages API, for the browser checks.

The app talks to it through the real SDK (ANTHROPIC_BASE_URL), so the checks
cover everything but the model: SDK parsing, the event stream through the
ASGI server, and the page rendering it as it arrives. It streams canned
answers slowly enough that a check can see the text grow.

    python e2e/fake_anthropic.py [port]        # default 8799
    FAKE_CHUNK_DELAY=0.05                       # seconds between fragments
"""
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DELAY = float(os.environ.get('FAKE_CHUNK_DELAY', '0.05'))

COVER_LETTER = """[SECTION: MAIN_LETTER]
Dear Northwind hiring team,

Your posting asks for someone who can keep payment APIs fast under load. At my last role I cut p95 latency from 900ms to 210ms on the invoicing API that serves 40 merchants.

You also need PostgreSQL depth. I designed the partitioning scheme that kept our ledger queries under 50ms as volume tripled.

I would welcome the chance to talk.

Maria Keller
[END_SECTION]

[SECTION: VERSION_A]
Dear Hiring Manager, I am applying for the Backend Engineer role at Northwind.
[END_SECTION]

[SECTION: VERSION_B]
900ms to 210ms. That is what I did to our invoicing API, and it is what I want to do for Northwind.
[END_SECTION]

[SECTION: ATS_ANALYSIS]
ATS score: 78/100. Missing keywords: **Kubernetes**, **Terraform**.
[END_SECTION]

[SECTION: RISK_ANALYSIS]
1. No Kubernetes experience listed. Fix: mention the container work at Northwind.
[END_SECTION]"""

ATS = """ATS COMPATIBILITY SCORE: 74/100

**KEYWORD MATCH**

| Matched | Missing |
|---|---|
| Python | Kubernetes |
| Django | Terraform |

**TOP 5 IMPROVEMENTS**
1. Add Kubernetes to the skills section.
2. Quantify the invoicing API work.
3. Move PostgreSQL higher.
4. Name the payment providers you integrated.
5. Cut the objective statement."""

INTERVIEW = """### Q1: Tell me about a latency problem you solved
**Why they ask:** Payments are latency-sensitive.
**Strong answer:** I cut p95 from 900ms to 210ms by removing N+1 queries and adding a read replica.
**Tip:** Lead with the number.

### Q2: How do you make a payment API idempotent?
**Why they ask:** Retries must not charge twice.
**Strong answer:** Idempotency keys stored with a unique constraint, and the first response replayed.
**Tip:** Mention the race between two retries."""

FOLLOWUP = """**3-Day Follow-Up**

**Subject:** Following up on my Backend Engineer application

Hi Northwind team, I applied for the Backend Engineer role on Monday and wanted to confirm it arrived.

**7-Day Follow-Up**

**Subject:** Still very interested in the Backend Engineer role

Hi again, I wanted to add one thing to my application: the invoicing API work."""


def answer_for(system):
    if 'cover letters' in system:
        return COVER_LETTER
    if 'ATS' in system:
        return ATS
    if 'interviewer' in system:
        return INTERVIEW
    return FOLLOWUP


def chunks(text, size=14):
    return [text[i:i + size] for i in range(0, len(text), size)]


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
        system = body.get('system') or ''
        if isinstance(system, list):
            system = ' '.join(b.get('text', '') for b in system)
        text = answer_for(system)
        model = body.get('model', 'claude-sonnet-5')
        usage = {'input_tokens': 1200, 'output_tokens': len(text) // 4}

        if not body.get('stream'):
            payload = json.dumps({
                'id': 'msg_fake', 'type': 'message', 'role': 'assistant', 'model': model,
                'content': [{'type': 'text', 'text': text}], 'stop_reason': 'end_turn',
                'stop_sequence': None, 'usage': usage,
            }).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'close')
        self.end_headers()

        def send(event, data):
            self.wfile.write(f'event: {event}\ndata: {json.dumps(data)}\n\n'.encode())
            self.wfile.flush()

        send('message_start', {'type': 'message_start', 'message': {
            'id': 'msg_fake', 'type': 'message', 'role': 'assistant', 'model': model, 'content': [],
            'stop_reason': None, 'stop_sequence': None, 'usage': {'input_tokens': usage['input_tokens'], 'output_tokens': 1}}})
        send('content_block_start', {'type': 'content_block_start', 'index': 0,
                                     'content_block': {'type': 'text', 'text': ''}})
        for piece in chunks(text):
            time.sleep(DELAY)
            send('content_block_delta', {'type': 'content_block_delta', 'index': 0,
                                         'delta': {'type': 'text_delta', 'text': piece}})
        send('content_block_stop', {'type': 'content_block_stop', 'index': 0})
        send('message_delta', {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn', 'stop_sequence': None},
                               'usage': {'output_tokens': usage['output_tokens']}})
        send('message_stop', {'type': 'message_stop'})
        self.close_connection = True


if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8799
    print(f'fake Anthropic API on http://127.0.0.1:{port}', flush=True)
    ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()
