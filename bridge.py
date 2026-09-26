import os
import sys
import time
import json
import uuid
import socket
import argparse
import traceback
import subprocess
import urllib.request
import threading
import asyncio
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import websockets
from websockets.protocol import State

from path_finder import find_chatgpt_executable

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(line_buffering=True)
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(line_buffering=True)

class CDPBridgeClient:
    def __init__(self, cdp_port=9223):
        self.cdp_port = cdp_port
        self.ws = None
        self.msg_id = 0
        self.current_conv_id = None
        self.lock = asyncio.Lock()

    def get_main_page_ws_url(self) -> str:
        url = f"http://127.0.0.1:{self.cdp_port}/json/list"
        req = urllib.request.urlopen(url, timeout=5)
        targets = json.loads(req.read().decode('utf-8'))
        
        # Primary match: main app window
        for t in targets:
            if t.get("type") == "page" and t.get("url") == "app://-/index.html":
                return t["webSocketDebuggerUrl"]
        # Fallback match: non-overlay page
        for t in targets:
            if t.get("type") == "page" and "webSocketDebuggerUrl" in t and "avatar-overlay" not in t.get("url", ""):
                return t["webSocketDebuggerUrl"]
                
        raise RuntimeError("No active ChatGPT main page found via CDP.")

    async def connect(self):
        if self.ws is not None and getattr(self.ws, 'state', None) == State.OPEN:
            return

        try:
            if self.ws:
                await self.ws.close()
        except Exception:
            pass
        self.ws = None

        ws_url = self.get_main_page_ws_url()
        print(f"[CDP] Connected to ChatGPT window: {ws_url}")
        self.ws = await websockets.connect(ws_url, max_size=16*1024*1024)

    async def call(self, method, params=None):
        for attempt in range(2):
            try:
                await self.connect()
                self.msg_id += 1
                current_id = self.msg_id
                payload = {"id": current_id, "method": method, "params": params or {}}
                await self.ws.send(json.dumps(payload))
                while True:
                    raw = await self.ws.recv()
                    resp = json.loads(raw)
                    if resp.get("id") == current_id:
                        return resp
            except Exception as e:
                print(f"[CDP] Call warning on attempt {attempt}: {e}")
                self.ws = None
                if attempt == 1:
                    raise
                await asyncio.sleep(0.5)

    async def ensure_chat_mode(self):
        """
        Guards against accidentally staying in Codex/Work mode.
        1. Checks and enforces the home mode toggle (聊天 vs 工作) to be '聊天' (Chat).
        2. Checks top-left dropdown (Codex vs ChatGPT).
        """
        js_guard = """
        (() => {
            const results = {};
            
            // 1. Check and enforce segmented toggle: "聊天" (Chat) vs "工作" (Work)
            const toggleContainer = document.querySelector('[class*="home-mode-toggle"]');
            if (toggleContainer) {
                const buttons = Array.from(toggleContainer.querySelectorAll('button'));
                const chatBtn = buttons.find(b => {
                    const text = (b.innerText || '').trim();
                    return text === '聊天' || text.toLowerCase() === 'chat' || b.className.includes('col-start-1');
                });
                if (chatBtn) {
                    const isPressed = chatBtn.getAttribute('aria-pressed') === 'true';
                    if (!isPressed) {
                        chatBtn.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, cancelable: true }));
                        chatBtn.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
                        chatBtn.click();
                        chatBtn.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true }));
                        results.toggle = 'switched_to_chat';
                    } else {
                        results.toggle = 'already_chat';
                    }
                }
            }
            
            // 2. Check top-left mode dropdown (Codex vs ChatGPT)
            const btns = Array.from(document.querySelectorAll('button'));
            const modeBtn = btns.find(b => (b.getAttribute('aria-label') || '').includes('模式') || (b.innerText || '').includes('ChatGPT') || (b.innerText || '').includes('Codex'));
            if (modeBtn) {
                const currentText = modeBtn.innerText || '';
                const currentAria = modeBtn.getAttribute('aria-label') || '';
                if (!currentText.includes('ChatGPT') && !currentAria.includes('ChatGPT')) {
                    modeBtn.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, cancelable: true }));
                    modeBtn.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
                    setTimeout(() => {
                        const options = Array.from(document.querySelectorAll('div, button, [role="menuitem"]'));
                        const chatgptOption = options.find(el => el.innerText && el.innerText.trim().startsWith('ChatGPT'));
                        if (chatgptOption) {
                            chatgptOption.click();
                        }
                    }, 300);
                    results.dropdown = 'switched_to_chat';
                } else {
                    results.dropdown = 'already_chat';
                }
            }
            
            return results;
        })()
        """
        res = await self.call("Runtime.evaluate", {"expression": js_guard, "returnByValue": True})
        return res.get("result", {}).get("result", {}).get("value")

    async def get_dom_conversation_id(self) -> str:
        js = """
        (() => {
            const convEl = document.querySelector('[data-response-annotation-conversation], [data-content-search-unit-key], [data-content-search-turn-key]');
            let cid = convEl?.getAttribute('data-response-annotation-conversation') || null;
            if (!cid && convEl) {
                const unitKey = convEl.getAttribute('data-content-search-unit-key') || convEl.getAttribute('data-content-search-turn-key');
                if (unitKey) {
                    cid = unitKey.split(':')[0];
                }
            }
            return cid;
        })()
        """
        res = await self.call("Runtime.evaluate", {"expression": js, "returnByValue": True})
        cid = res.get("result", {}).get("result", {}).get("value")
        return cid or ""

    async def trigger_new_chat(self):
        js = """
        (() => {
            const btns = Array.from(document.querySelectorAll('button, a'));
            const newChatBtn = btns.find(b => {
                const label = (b.getAttribute('aria-label') || '').toLowerCase();
                const text = (b.innerText || '').toLowerCase();
                return label.includes('新聊天') || text.includes('新聊天') || label.includes('新对话') || text.includes('新对话') || label.includes('new chat');
            });
            if (newChatBtn) {
                newChatBtn.click();
                return true;
            }
            return false;
        })()
        """
        await self.call("Runtime.evaluate", {"expression": js, "returnByValue": True})
        await asyncio.sleep(0.5)
        # Enforce chat mode switch upon entering new chat home screen
        await self.ensure_chat_mode()
        await asyncio.sleep(0.2)


    async def set_reasoning_effort(self, target_level: str) -> bool:
        """
        Dynamically adjusts model reasoning effort:
        - 'instant' / 'low' / '即时' -> 0
        - 'medium' / '中' -> 1
        - 'high' / '高' -> 2
        """
        level_map = {
            'instant': 0, 'low': 0, '即时': 0, '0': 0,
            'medium': 1, '中': 1, '1': 1,
            'high': 2, '高': 2, '2': 2
        }
        if str(target_level).lower() not in level_map:
            return False
        target_val = level_map[str(target_level).lower()]

        js = f"""
        (() => {{
            const btns = Array.from(document.querySelectorAll('button'));
            const targetBtn = btns.find(b => (b.getAttribute('aria-label') || '').includes('模型') || ['即时', '中', '高'].includes((b.innerText || '').trim()));
            if (!targetBtn) return {{ error: 'Model/Effort button not found' }};
            
            // Open the model/effort dropdown if not already open
            let menu = document.querySelector('[role="menu"][data-state="open"]');
            if (!menu) {{
                targetBtn.dispatchEvent(new PointerEvent('pointerdown', {{ bubbles: true, cancelable: true }}));
                targetBtn.dispatchEvent(new MouseEvent('mousedown', {{ bubbles: true, cancelable: true }}));
                targetBtn.click();
            }}
            
            return new Promise(resolve => {{
                setTimeout(() => {{
                    const slider = document.querySelector('[role="slider"]');
                    if (!slider) {{
                        resolve({{ error: 'Slider not found in menu' }});
                        return;
                    }}
                    
                    let cur = parseInt(slider.getAttribute('aria-valuenow') || '2', 10);
                    const target = {target_val};
                    
                    while (cur > target) {{
                        slider.dispatchEvent(new KeyboardEvent('keydown', {{ key: 'ArrowLeft', code: 'ArrowLeft', keyCode: 37, which: 37, bubbles: true, cancelable: true }}));
                        slider.dispatchEvent(new KeyboardEvent('keyup', {{ key: 'ArrowLeft', code: 'ArrowLeft', keyCode: 37, which: 37, bubbles: true, cancelable: true }}));
                        cur--;
                    }}
                    while (cur < target) {{
                        slider.dispatchEvent(new KeyboardEvent('keydown', {{ key: 'ArrowRight', code: 'ArrowRight', keyCode: 39, which: 39, bubbles: true, cancelable: true }}));
                        slider.dispatchEvent(new KeyboardEvent('keyup', {{ key: 'ArrowRight', code: 'ArrowRight', keyCode: 39, which: 39, bubbles: true, cancelable: true }}));
                        cur++;
                    }}
                    
                    setTimeout(() => {{
                        document.dispatchEvent(new KeyboardEvent('keydown', {{ key: 'Escape', code: 'Escape', keyCode: 27, bubbles: true }}));
                        resolve({{ success: true, target: target }});
                    }}, 100);
                }}, 300);
            }});
        }})()
        """
        res = await self.call("Runtime.evaluate", {"expression": js, "awaitPromise": True, "returnByValue": True})
        val = res.get("result", {}).get("result", {}).get("value", {})
        return bool(val.get("success"))

    async def send_chat_message(self, prompt: str, conversation_id: str = None, effort: str = None, timeout_seconds: float = 75.0) -> dict:
        async with self.lock:
            await self.connect()
            await self.ensure_chat_mode()

            # If reasoning effort is specified, adjust it before prompt submission
            if effort:
                await self.set_reasoning_effort(effort)

            current_dom_cid = await self.get_dom_conversation_id()
            if current_dom_cid:
                self.current_conv_id = current_dom_cid

            # Determine whether to create a new chat
            needs_new_chat = False
            if not conversation_id or conversation_id.lower() == "new":
                needs_new_chat = True
            elif self.current_conv_id and conversation_id != self.current_conv_id:
                print(f"[Bridge] Target conv_id ({conversation_id}) != current ({self.current_conv_id}), creating new conversation...")
                needs_new_chat = True

            if needs_new_chat:
                await self.trigger_new_chat()
                if effort:
                    await self.set_reasoning_effort(effort)

            # 1. Insert prompt using Selection & execCommand (preserves Unicode & multi-lines)
            insert_js = f"""
            (() => {{
                const el = document.querySelector('div.ProseMirror');
                if (!el) return false;
                el.focus();
                const sel = window.getSelection();
                const range = document.createRange();
                range.selectNodeContents(el);
                sel.removeAllRanges();
                sel.addRange(range);
                document.execCommand('insertText', false, {json.dumps(prompt)});
                return true;
            }})()
            """
            await self.call("Runtime.evaluate", {"expression": insert_js, "returnByValue": True})
            await asyncio.sleep(0.2)

            # 2. Count existing assistant responses before sending
            count_js = "document.querySelectorAll('[data-markdown-text-style=\"assistant-message\"]').length"
            res = await self.call("Runtime.evaluate", {"expression": count_js, "returnByValue": True})
            prev_msg_count = res.get("result", {}).get("result", {}).get("value", 0)

            # 3. Click Send button
            click_send_js = """
            (() => {
                const btns = Array.from(document.querySelectorAll('button'));
                const sendBtn = btns.find(b => {
                    const l = (b.getAttribute('aria-label') || '').toLowerCase();
                    const t = (b.getAttribute('data-testid') || '').toLowerCase();
                    return (l.includes('发送') || l.includes('send') || t.includes('send')) && !b.disabled;
                });
                if (sendBtn) {
                    sendBtn.click();
                    return true;
                }
                return false;
            })()
            """
            send_res = await self.call("Runtime.evaluate", {"expression": click_send_js, "returnByValue": True})
            if not send_res.get("result", {}).get("result", {}).get("value"):
                await self.call("Input.dispatchKeyEvent", {"type": "keyDown", "windowsVirtualKeyCode": 13, "text": "\r"})
                await self.call("Input.dispatchKeyEvent", {"type": "keyUp", "windowsVirtualKeyCode": 13})

            # 4. Polling for response completion and capturing new conversation_id
            start_time = time.time()
            final_text = ""
            stable_count = 0
            last_seen_text = ""
            captured_cid = None

            while time.time() - start_time < timeout_seconds:
                await asyncio.sleep(0.5)

                check_js = f"""
                (() => {{
                    const msgs = document.querySelectorAll('[data-markdown-text-style="assistant-message"]');
                    const btns = Array.from(document.querySelectorAll('button'));
                    const isStopVisible = btns.some(b => {{
                        const l = (b.getAttribute('aria-label') || '').toLowerCase();
                        const t = (b.getAttribute('data-testid') || '').toLowerCase();
                        return l.includes('停止') || l.includes('stop') || t.includes('stop');
                    }});
                    const latestMsg = msgs.length > {prev_msg_count} ? msgs[msgs.length - 1].innerText : '';
                    
                    const convEl = document.querySelector('[data-response-annotation-conversation], [data-content-search-unit-key], [data-content-search-turn-key]');
                    let cid = convEl?.getAttribute('data-response-annotation-conversation') || null;
                    if (!cid && convEl) {{
                        const unitKey = convEl.getAttribute('data-content-search-unit-key') || convEl.getAttribute('data-content-search-turn-key');
                        if (unitKey) {{
                            cid = unitKey.split(':')[0];
                        }}
                    }}

                    return {{
                        hasNewMsg: msgs.length > {prev_msg_count},
                        isGenerating: isStopVisible,
                        text: latestMsg,
                        convId: cid
                    }};
                }})()
                """
                check_res = await self.call("Runtime.evaluate", {"expression": check_js, "returnByValue": True})
                info = check_res.get("result", {}).get("result", {}).get("value", {})
                
                has_new = info.get("hasNewMsg", False)
                is_gen = info.get("isGenerating", False)
                cur_text = info.get("text", "").strip()
                if info.get("convId"):
                    captured_cid = info.get("convId")

                if has_new and cur_text:
                    if cur_text == last_seen_text:
                        stable_count += 1
                    else:
                        stable_count = 0
                        last_seen_text = cur_text

                    # Response generated and content stabilized for 1s
                    if not is_gen and stable_count >= 2:
                        final_text = cur_text
                        break

            if captured_cid:
                self.current_conv_id = captured_cid

            return {
                "response": final_text if final_text else last_seen_text,
                "conversation_id": self.current_conv_id
            }

cdp_client = None
event_loop = None

class BridgeHTTPHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        valid_paths = ["/chat", "/v1/chat", "/v1/chat/completions", "/chat/completions"]
        if self.path in valid_paths:
            try:
                length = int(self.headers.get('content-length', 0))
                body = self.rfile.read(length).decode('utf-8')
                data = json.loads(body)
            except Exception as e:
                self.send_error(400, f"Invalid JSON payload: {e}")
                return

            # Extract prompt from 'prompt' or 'messages'
            user_prompt = data.get("prompt")
            if not user_prompt:
                messages = data.get("messages", [])
                for m in reversed(messages):
                    if m.get("role") == "user":
                        user_prompt = m.get("content", "")
                        break
                if not user_prompt and messages:
                    user_prompt = messages[-1].get("content", "")

            if not user_prompt:
                user_prompt = "Hello"

            conversation_id = data.get("conversation_id")

            # Extract reasoning effort if provided (instant / medium / high)
            effort = data.get("effort") or data.get("reasoning_effort")
            if not effort and isinstance(data.get("model"), str):
                m_lower = data.get("model").lower()
                for e in ["instant", "medium", "high"]:
                    if e in m_lower:
                        effort = e
                        break

            print(f"[Bridge] Request: prompt='{user_prompt[:40]}...', conv_id={conversation_id}, effort={effort}")

            future = asyncio.run_coroutine_threadsafe(
                cdp_client.send_chat_message(user_prompt, conversation_id=conversation_id, effort=effort),
                event_loop
            )
            try:
                result = future.result(timeout=80)
            except Exception as e:
                print(f"[Bridge] Request processing error: {e}")
                traceback.print_exc()
                try:
                    self.send_error(500, f"CDP Bridge error: {e}")
                except Exception:
                    pass
                return

            reply_text = result["response"]
            cid = result["conversation_id"]

            resp_obj = {
                "conversation_id": cid,
                "response": reply_text,
                "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": data.get("model", "chatgpt-desktop"),
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": reply_text
                        },
                        "finish_reason": "stop"
                    }
                ],
                "usage": {
                    "prompt_tokens": len(user_prompt),
                    "completion_tokens": len(reply_text),
                    "total_tokens": len(user_prompt) + len(reply_text)
                }
            }

            try:
                resp_bytes = json.dumps(resp_obj, ensure_ascii=False).encode('utf-8')
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(resp_bytes)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "*")
                self.end_headers()
                self.wfile.write(resp_bytes)
            except Exception as e:
                print(f"[Bridge] Write response failed: {e}")
        else:
            self.send_error(404, "Not Found")

    def do_GET(self):
        if self.path in ["/", "/health", "/status"]:
            status_data = {
                "status": "ok",
                "service": "chatgpt-desktop-bridge",
                "current_conversation_id": cdp_client.current_conv_id
            }
            resp_bytes = json.dumps(status_data, indent=2).encode('utf-8')
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp_bytes)))
            self.end_headers()
            self.wfile.write(resp_bytes)
        elif self.path in ["/v1/models", "/models"]:
            models_data = {
                "object": "list",
                "data": [
                    {"id": "gpt-5.6-sol", "object": "model", "owned_by": "openai"},
                    {"id": "gpt-5.6-sol-high", "object": "model", "owned_by": "openai"},
                    {"id": "gpt-5.6-sol-medium", "object": "model", "owned_by": "openai"},
                    {"id": "gpt-5.6-sol-instant", "object": "model", "owned_by": "openai"},
                    {"id": "chatgpt-desktop", "object": "model", "owned_by": "openai"}
                ]
            }
            resp_bytes = json.dumps(models_data, indent=2).encode('utf-8')
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp_bytes)))
            self.end_headers()
            self.wfile.write(resp_bytes)
        else:
            self.send_error(404, "Not Found")

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def log_message(self, format, *args):
        return

def is_port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('127.0.0.1', port)) == 0

def ensure_chatgpt_running(cdp_port: int, custom_path: str = None, user_data_dir: str = None, auto_launch: bool = True):
    if is_port_in_use(cdp_port):
        print(f"[+] ChatGPT Desktop is already running and listening on CDP port {cdp_port}")
        return True

    if not auto_launch:
        print(f"[!] Warning: Port {cdp_port} is not listening and --no-launch is set.")
        return False

    print(f"[*] Port {cdp_port} is not listening. Searching for ChatGPT executable...")
    app_path = find_chatgpt_executable(custom_path)
    if not app_path:
        print(f"[-] Error: Could not locate ChatGPT Desktop installation.")
        print(f"    Please specify the path using:")
        print(f"      1. CLI argument: python bridge.py --chatgpt-path <path>")
        print(f"      2. Environment variable: set CHATGPT_PATH=<path>")
        return False

    print(f"[+] Found ChatGPT executable: {app_path}")
    
    # Terminate existing non-debugging instance if running
    if sys.platform == "win32":
        try:
            subprocess.run(["powershell", "-NoProfile", "-Command", "Stop-Process -Name 'ChatGPT' -Force -ErrorAction SilentlyContinue"], capture_output=True)
            time.sleep(2)
        except Exception:
            pass

    args = [app_path, f"--remote-debugging-port={cdp_port}"]
    if user_data_dir:
        os.makedirs(user_data_dir, exist_ok=True)
        print(f"[*] Using custom user data directory: {user_data_dir}")
        args.append(f"--user-data-dir={user_data_dir}")

    print(f"[*] Launching ChatGPT with: {' '.join(args)}...")
    subprocess.Popen(args)
    
    # Wait for CDP port to open
    for _ in range(20):
        time.sleep(1)
        if is_port_in_use(cdp_port):
            print(f"[+] ChatGPT Desktop started successfully on CDP port {cdp_port}")
            return True

    print(f"[-] Failed to detect CDP port {cdp_port} after launching ChatGPT.")
    return False

def main():
    global cdp_client, event_loop

    parser = argparse.ArgumentParser(description="ChatGPT Desktop CDP OpenAI-Compatible Bridge")
    parser.add_argument("--port", type=int, default=18080, help="Local HTTP server port (default: 18080)")
    parser.add_argument("--cdp-port", type=int, default=9223, help="ChatGPT CDP remote debugging port (default: 9223)")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Local listen host (default: 127.0.0.1)")
    parser.add_argument("--chatgpt-path", type=str, default="", help="Custom path to ChatGPT.exe / ChatGPT.app")
    parser.add_argument("--user-data-dir", type=str, default="", help="Custom user data directory (isolated profile)")
    parser.add_argument("--isolated-profile", action="store_true", help="Use a dedicated isolated user data directory (~/.chatgpt-desktop-bridge-profile)")
    parser.add_argument("--default-effort", type=str, choices=["instant", "medium", "high"], default=None, help="Set default reasoning effort (instant / medium / high)")
    parser.add_argument("--no-launch", action="store_true", help="Do not automatically launch ChatGPT Desktop")
    args = parser.parse_args()

    # Determine user data dir if requested
    user_data_dir = args.user_data_dir.strip()
    if not user_data_dir and args.isolated_profile:
        user_data_dir = os.path.join(os.path.expanduser("~"), ".chatgpt-desktop-bridge-profile")

    # Step 1: Ensure ChatGPT is running with CDP
    if not ensure_chatgpt_running(args.cdp_port, custom_path=args.chatgpt_path, user_data_dir=user_data_dir, auto_launch=not args.no_launch):
        print("[-] Aborting bridge startup due to missing ChatGPT CDP connection.")
        sys.exit(1)

    # Step 2: Initialize CDP client
    cdp_client = CDPBridgeClient(cdp_port=args.cdp_port)
    event_loop = asyncio.new_event_loop()

    def run_async_loop():
        asyncio.set_event_loop(event_loop)
        event_loop.run_forever()

    threading.Thread(target=run_async_loop, daemon=True).start()

    # Step 2.5: Apply default effort if configured
    if args.default_effort:
        future = asyncio.run_coroutine_threadsafe(
            cdp_client.set_reasoning_effort(args.default_effort),
            event_loop
        )
        try:
            future.result(timeout=10)
            print(f"[+] Initial reasoning effort set to: {args.default_effort}")
        except Exception as e:
            print(f"[!] Warning: Failed to set initial reasoning effort: {e}")

    # Step 3: Run multi-threaded HTTP server
    print("==================================================================")
    print(f"  ChatGPT Desktop Bridge is RUNNING on http://{args.host}:{args.port}")
    print(f"  Chat Endpoint:     http://{args.host}:{args.port}/chat")
    print(f"  OpenAI Endpoint:   http://{args.host}:{args.port}/v1/chat/completions")
    print(f"  Models Endpoint:   http://{args.host}:{args.port}/v1/models")
    print(f"  Health Check:      http://{args.host}:{args.port}/health")
    print("==================================================================")

    while True:
        try:
            server = ThreadingHTTPServer((args.host, args.port), BridgeHTTPHandler)
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n[+] Bridge server stopped.")
            break
        except Exception as e:
            print(f"[!] Server exception: {e}, restarting in 2 seconds...")
            traceback.print_exc()
            time.sleep(2)

if __name__ == "__main__":
    main()
