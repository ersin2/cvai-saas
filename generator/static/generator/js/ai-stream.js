/* Read an AI answer as it is written.

   CVAIStream.post(url, formData, handlers) POSTs the form asking for
   text/event-stream (generator/views.py, _event_stream) and calls:

     onDelta(textSoFar)   for each new fragment
     onDone(payload)      once, with what the JSON answer would have been
     onError(message)     once, if generation failed — discard what was shown
     onHttpError(resp)    for a non-2xx answer (402 quota, 429 throttle, …),
                          which is always plain JSON, never a stream

   A server that answers with JSON instead of a stream is handled too: the
   whole result arrives through onDone (or onError).

   CVAIStream.eachFrame(fn) wraps a painter so it runs at most once per
   animation frame with the latest value; call .cancel() before the final
   render, or a queued frame could paint a partial answer over it. */
(function () {
  'use strict';

  const EMPTY = 'AI returned an empty response. Please try again.';
  const CUT_OFF = 'The connection closed before the answer was finished. Please try again.';

  async function post(url, body, handlers) {
    const resp = await fetch(url, {
      method: 'POST',
      body: body,
      headers: { 'Accept': 'text/event-stream' },
      credentials: 'same-origin',
    });
    if (!resp.ok) { await handlers.onHttpError(resp); return; }

    const type = resp.headers.get('Content-Type') || '';
    if (!type.includes('text/event-stream') || !resp.body) {
      const data = await resp.json();
      if (data.error || !data.result) handlers.onError(data.error || EMPTY);
      else handlers.onDone(data);
      return;
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let text = '';
    let finished = false;

    function dispatch(block) {
      let event = 'message';
      let data = '';
      block.split('\n').forEach(function (line) {
        if (line.startsWith('event:')) event = line.slice(6).trim();
        else if (line.startsWith('data:')) data += line.slice(5).trim();
      });
      if (!data || finished) return;          // comments (keep-alives) carry no data
      const payload = JSON.parse(data);
      if (event === 'delta') {
        text += payload.text;
        if (handlers.onDelta) handlers.onDelta(text);
      } else if (event === 'done') {
        finished = true;
        handlers.onDone(payload);
      } else if (event === 'error') {
        finished = true;
        handlers.onError(payload.error || EMPTY);
      }
    }

    for (;;) {
      const chunk = await reader.read();
      if (chunk.done) break;
      buffer += decoder.decode(chunk.value, { stream: true }).replace(/\r\n/g, '\n');
      let cut;
      while ((cut = buffer.indexOf('\n\n')) >= 0) {
        dispatch(buffer.slice(0, cut));
        buffer = buffer.slice(cut + 2);
      }
    }
    if (buffer.trim()) dispatch(buffer);
    if (!finished) handlers.onError(CUT_OFF);
  }

  function eachFrame(paint) {
    let latest;
    let queued = false;
    let cancelled = false;
    function schedule(value) {
      latest = value;
      if (queued || cancelled) return;
      queued = true;
      requestAnimationFrame(function () {
        queued = false;
        if (!cancelled) paint(latest);
      });
    }
    schedule.cancel = function () { cancelled = true; };
    return schedule;
  }

  window.CVAIStream = { post: post, eachFrame: eachFrame };
})();
