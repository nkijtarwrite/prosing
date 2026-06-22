import http.server
import json
import urllib.request
import urllib.error
import socketserver
import sys
import os

# ==================================================
# 🔑 請在下方填入您的真實 Gemini API Key (以 AIzaSy 開頭)
# ==================================================
GEMINI_API_KEY = "AIzaSyCFrDmTadlvqogmFyv7U4xNJaSQzyGMXqo"

class GeminiProxyHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # 隱藏預設的終端機連線紀錄，保持排版乾淨
        pass

    def do_POST(self):
        print(f"\n[收到請求] {self.path}")
        
        content_length = int(self.headers.get('Content-Length', 0))
        req_body = self.rfile.read(content_length)
        
        try:
            payload = json.loads(req_body.decode('utf-8'))
        except Exception as e:
            self.send_error(400, f"Invalid JSON: {str(e)}")
            return

        # 1. 攔截請求：自動注入 Google 官方的免簽驗證標記
        if "messages" in payload:
            for message in payload["messages"]:
                if message.get("role") == "assistant" and "tool_calls" in message:
                    for tool_call in message["tool_calls"]:
                        if "extra_content" not in tool_call:
                            tool_call["extra_content"] = {
                                "google": {
                                    "thought_signature": "skip_thought_signature_validator"
                                }
                            }

        # 2. 準備轉發至 Google AI Studio
        google_url = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
        
        # 準備請求標頭
        headers = {}
        for key, val in self.headers.items():
            key_lower = key.lower()
            if key_lower == "content-type":
                headers["Content-Type"] = val
            elif key_lower == "accept":
                headers["Accept"] = val

        # 3. 處理 Authorization（API Key 驗證）
        client_auth = self.headers.get('Authorization') or self.headers.get('authorization')
        
        if GEMINI_API_KEY and not GEMINI_API_KEY.startswith("您的真實"):
            headers["Authorization"] = f"Bearer {GEMINI_API_KEY}"
            masked_key = GEMINI_API_KEY[:8] + "..." + GEMINI_API_KEY[-4:] if len(GEMINI_API_KEY) > 12 else "..."
            print(f"  -> [本地注入] 已使用真實金鑰 (Bearer {masked_key})")
        elif client_auth:
            headers["Authorization"] = client_auth
            masked_client = client_auth[:15] + "..." if len(client_auth) > 15 else client_auth
            print(f"  -> [轉發] 使用 VS Code 帶入的驗證資訊: {masked_client}")
        else:
            print("  -> [錯誤] 未提供 API Key 驗證標頭！")

        modified_data = json.dumps(payload).encode('utf-8')
        headers["Content-Length"] = str(len(modified_data))

        req = urllib.request.Request(
            url=google_url,
            data=modified_data,
            headers=headers,
            method="POST"
        )

        try:
            # 4. 發送請求並以「串流（Streaming）」方式回傳給 VS Code
            with urllib.request.urlopen(req) as resp:
                self.send_response(resp.status)
                for key, val in resp.getheaders():
                    if key.lower() not in ["transfer-encoding", "connection", "content-length"]:
                        self.send_header(key, val)
                self.end_headers()

                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
                print("  -> [成功] 請求處理完成並成功串流回傳。")

        except urllib.error.HTTPError as e:
            err_body = e.read()
            
            # 🚨 關鍵改動：如果上游返回 429 限流錯誤，進行「優雅降級」攔截 🚨
            if e.code == 429:
                print("  -> [優雅降級] 偵測到 429 限流！正在包裝為友善 Markdown 回覆給 VS Code...")
                try:
                    err_json = json.loads(err_body.decode('utf-8'))
                    raw_msg = err_json.get("error", {}).get("message", "無詳細說明")
                except Exception:
                    raw_msg = err_body.decode('utf-8', errors='ignore')
                
                # 建立在 VS Code Chat 視窗中顯示的排版訊息
                friendly_tips = (
                    f"⚠️ **[本地代理提示] 觸發 Google AI Studio 額度上限 (429 Rate Limit)**\n\n"
                    f"**錯誤詳情：**\n"
                    f"> {raw_msg}\n\n"
                    f"---\n"
                    f"**💡 您可以嘗試以下步驟來立即恢復使用：**\n"
                    f"1. **清空歷史對話**：點擊聊天視窗右上角的 **`+` (New Chat)** 按鈕。這可以立即丟棄先前的對話歷史，減少 90% 以上的輸入 Token！\n"
                    f"2. **關閉超大檔案**：關閉目前在編輯器中開啟、但與當前問題不相關且代碼極長（上千行）的檔案分頁。\n"
                    f"3. **切換高額度模型**：如果頻繁遇到此問題，建議在 VS Code 中切換至 `gemini-1.5-flash`（該模型在免費層級擁有 1,000,000 TPM 的額度，是 3.1-flash-lite 的 4 倍）。"
                )
                
                is_stream = payload.get("stream", False)
                
                # 偽裝成 HTTP 200 回應給 VS Code
                self.send_response(200)
                if is_stream:
                    self.send_header('Content-Type', 'text/event-stream')
                    self.send_header('Cache-Control', 'no-cache')
                    self.send_header('Connection', 'keep-alive')
                    self.end_headers()
                    
                    # 模擬 OpenAI 串流格式將 Markdown 吐給 VS Code
                    chunk = {
                        "choices": [{
                            "delta": {
                                "content": friendly_tips
                            },
                            "finish_reason": "stop",
                            "index": 0
                        }],
                        "object": "chat.completion.chunk"
                    }
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode('utf-8'))
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                else:
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    
                    resp_json = {
                        "choices": [{
                            "message": {
                                "role": "assistant",
                                "content": friendly_tips
                            },
                            "finish_reason": "stop",
                            "index": 0
                        }],
                        "object": "chat.completion"
                    }
                    self.wfile.write(json.dumps(resp_json).encode('utf-8'))
                    self.wfile.flush()
                print("  -> [優雅降級] 已成功將 429 提示呈現在對話視窗中。")
                return
            
            # 其他非 429 錯誤（例如 400 或 500）保持原樣轉發
            print(f"  -> [上游錯誤] Google AI Studio 回報 HTTP {e.code}")
            self.send_response(e.code)
            for key, val in e.headers.items():
                if key.lower() not in ["transfer-encoding", "connection", "content-length"]:
                    self.send_header(key, val)
            self.end_headers()
            self.wfile.write(err_body)
            self.wfile.flush()
            
        except Exception as e:
            print(f"  -> [代理錯誤] {str(e)}")
            self.send_error(500, f"Proxy error: {str(e)}")

PORT = 3000
handler = GeminiProxyHandler
socketserver.TCPServer.allow_reuse_address = True

print(f"==================================================")
print(f" Gemini 3.1 代理伺服器 (429 限流優雅降級版) 已啟動")
print(f" 本地接聽網址: http://localhost:{PORT}")
print(f"==================================================")

with socketserver.TCPServer(("", PORT), handler) as httpd:
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止代理服務...")
        httpd.server_close()
        sys.exit(0)