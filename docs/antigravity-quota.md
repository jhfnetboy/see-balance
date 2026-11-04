# Antigravity (AGY) 配额查询说明

## 1. 凭据获取 (OAuth Token)

- **Keychain 获取**：
  ```bash
  security find-generic-password -s gemini -a antigravity -w
  ```
  结果去掉前缀 `go-keyring-base64:` 后进行 Base64 解码，提取 `token.access_token` 与 `token.refresh_token`。
- **本地凭据文件**：`~/.gemini/oauth_creds.json`
- **环境变量覆盖**：`AGY_ACCESS_TOKEN` 或 `GEMINI_OAUTH_TOKEN`

## 2. Token 刷新

- **URL**：`POST https://oauth2.googleapis.com/token`
- **Body**：
  ```json
  {
    "client_id": "<AGY_CLIENT_ID>",
    "client_secret": "<AGY_CLIENT_SECRET>",
    "grant_type": "refresh_token",
    "refresh_token": "<REFRESH_TOKEN>"
  }
  ```

## 3. 获取用户套餐与 Project ID

- **URL**：`POST https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist`
  （备用：`https://daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist`）
- **Headers**：
  - `Authorization: Bearer <ACCESS_TOKEN>`
  - `Content-Type: application/json`
  - `User-Agent: Antigravity/1.0`
- **Body**：
  ```json
  {
    "mode": "HEALTH_CHECK",
    "metadata": {
      "ideType": "ANTIGRAVITY",
      "platform": "DARWIN_ARM64"
    }
  }
  ```
- **解析字段**：
  - `cloudaicompanionProject`（默认为 `aicode-consumers`）
  - `paidTier` / `currentTier` 套餐与 credits。

## 4. 查询实际配额桶 (Quota Summary)

- **URL**：`POST https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary`
  （备用：`https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary`）
- **Headers**：同上
- **Body**：
  ```json
  {
    "project": "<PROJECT_ID>"
  }
  ```
- **结构解析**：
  - 配额桶位于 `groups[].buckets[]`。
  - **Gemini 池**：`gemini-5h` / `gemini-weekly`
  - **3P 模型池（Claude / GPT）**：`3p-5h` / `3p-weekly`
  - **字段**：
    - `remainingFraction`：剩余比例（0.0 ~ 1.0，按 token 成本扣除）
    - `resetTime`：重置时间的 ISO 8601 字符串
