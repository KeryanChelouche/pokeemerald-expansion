#!/usr/bin/env python3
"""Serve the emulator's current frame as a page you can watch while you play.

The harness runs mgba-headless, which has no window -- that is the whole reason
it can be scripted at all, since no released mGBA frontend takes --script. So
"watch the screen" cannot mean a real emulator window without giving up the
scripting the harness is built on.

What it can mean: the ROM already renders to a framebuffer (the local mGBA patch
in tools/), the Lua already writes it out for recording, and a browser is a
perfectly good screen. This serves the latest frame at localhost and refreshes
it, which is plenty for a turn-based game where the interesting question is
"what is happening in this battle", not "can I react in 16ms".

Started automatically by campaign.py --live; not usually run by hand.
"""

import http.server
import pathlib
import socketserver
import threading

PAGE = """<!doctype html>
<meta charset="utf-8">
<title>nuzlocke — live</title>
<style>
  html, body { margin:0; height:100%; background:#101014; color:#c8c8d0;
               font:13px ui-monospace, monospace; }
  body { display:flex; flex-direction:column; align-items:center;
         justify-content:center; gap:10px; }
  /* Nearest-neighbour: a GBA frame is 240x160 and smoothing it just makes the
     text unreadable. */
  img { width:min(96vw, 960px); image-rendering:pixelated;
        border:1px solid #2a2a33; border-radius:4px; background:#000; }
  #s { opacity:.55 }
</style>
<img id="v" alt="waiting for the first frame…">
<div id="s">connecting…</div>
<script>
const img = document.getElementById('v'), st = document.getElementById('s');
let n = 0, misses = 0;
// Poll rather than push: no websocket, no dependency, and the page survives the
// emulator restarting between runs.
async function tick() {
  const next = new Image();
  next.onload = () => { img.src = next.src; n++; misses = 0;
                        st.textContent = 'live · ' + n + ' frames'; };
  next.onerror = () => { misses++;
                         st.textContent = misses > 8 ? 'emulator not running'
                                                     : 'waiting…'; };
  next.src = '/live.png?t=' + Date.now();
}
setInterval(tick, __MS__);
tick();
</script>
"""


class _Handler(http.server.SimpleHTTPRequestHandler):
    directory: str = "."
    refresh_ms: int = 150

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=self.directory, **kw)

    def do_GET(self):                                    # noqa: N802
        if self.path == "/" or self.path.startswith("/?"):
            body = PAGE.replace("__MS__", str(self.refresh_ms)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        return super().do_GET()

    def end_headers(self):
        # The frame changes constantly, so a cached one is worse than none.
        # Set here rather than in do_GET: headers must follow send_response, and
        # SimpleHTTPRequestHandler issues that itself for static files.
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, *a):
        pass                                             # the console is the game


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def serve(frame_dir: pathlib.Path, port: int = 8420) -> str | None:
    """Start the viewer in a background thread. Returns the URL, or None.

    Never fatal: a live view is a convenience, and losing a run because the port
    was busy would be a poor trade.
    """
    _Handler.directory = str(frame_dir)
    try:
        srv = _Server(("127.0.0.1", port), _Handler)
    except OSError as e:
        print(f"  live view unavailable on port {port}: {e}")
        return None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{port}/"


if __name__ == "__main__":
    import sys
    d = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    url = serve(d, int(sys.argv[2]) if len(sys.argv) > 2 else 8420)
    print(f"serving {d} at {url}")
    threading.Event().wait()
