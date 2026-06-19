nano risk_monitor.py

.\.venv\Scripts\activate

.\start_aider.bat

aider --model deepseek/deepseek-chat app.py

ssh -i "aws-key-sydney.pem" ubuntu@<EC2_PUBLIC_IP>


##################


這是一個典型的 **Python 安全機制 (PEP 668)**。

現代的 Linux 系統（如 Ubuntu 24.04+）為了防止你亂裝套件把系統搞掛，禁止直接使用 `pip install` 安裝到系統路徑。

既然你在學習資料科學，我們就用最專業的方法：**建立虛擬環境 (Virtual Environment)**。這能確保你的專案套件是獨立的，不會跟系統衝突。

請一行一行執行以下指令：

### 1. 安裝虛擬環境工具

```bash
sudo apt update && sudo apt install python3-venv -y

```

### 2. 建立你的專案資料夾並進入

```bash
mkdir my_monitor && cd my_monitor

```

### 3. 建立並啟動虛擬環境

```bash
python3 -m venv venv
source venv/bin/activate

```

*執行完後，你會發現提示字串前面多了 `(venv)`，這代表你現在是在一個安全的隔離空間裡。*

### 4. 現在可以安裝套件了

```bash
pip install ccxt requests

```

*(注意：`statistics` 是 Python 內建庫，不需另外安裝)*

### 5. 建立程式碼檔案

```bash
nano risk_monitor.py

```

*這會打開編輯器。請將之前整合了 Telegram 的 Python 程式碼貼進去。*

* **存檔：** `Ctrl + O` 然後 `Enter`
* **退出：** `Ctrl + X`

### 6. 測試運行

在正式啟動後台運行前，先跑一次看會不會報錯：

```bash
python risk_monitor.py

```

* **如果成功：** 你會看到 `Spread: ...` 的 log 滾動，且你的 Telegram 應該會收到一條「監控系統已啟動」的訊息。
* **停止測試：** 按 `Ctrl + C`。

---

### 7. 正式後台執行 (這才是關鍵)

確認測試沒問題後，用這行讓它在後台永生：

```bash
nohup python -u risk_monitor.py > output.log 2>&1 &

```

**檢查是否真的在跑：**

```bash
cat output.log

```

如果你看到 log 在跑，恭喜你，你已經完成了從 **「本地開發」** 到 **「雲端自動化」** 的全過程。

**下一步：**
如果 Telegram 收到訊息了，截圖給我看，或者告訴我 Spread 是多少。這代表你在雪梨的第一個自動化風險哨兵正式站崗了。

python3 -c "import os, requests; print(requests.post(f\"https://api.telegram.org/bot{os.environ['TG_TOKEN']}/sendMessage\", data={'chat_id': os.environ['TG_CHAT_ID'], 'text': '✅ 來自 AWS 的測試訊息：系統連線成功！'}).json())"

###########

這就是我剛才最後為什麼急著要教你 **Systemd** 的原因。

先別急著否定你的努力。**AWS 的電腦確實還在運作（它沒關機），但是你的程式因為「黏在你的連線視窗上」，所以跟著視窗一起關了。**

用個最白話的比喻：

* **你之前的做法（直接跑 python）：** 像是你打電話給 AWS，叫它在電話裡唸書給你聽。你一掛電話（關電腦），它就停了。
* **AWS 的運作：** 電話那一頭的員工（伺服器）其實還坐在那裡沒下班，只是因為你掛電話了，它就停止唸書了。
* **我們現在要做的（Systemd）：** 是寄一封信給 AWS，命令它：「不管我有沒有打電話過來，你都要把這本書唸完，唸到死為止。」

你昨天的挫折，是因為我們還沒做完最後這一步 **「切斷臍帶」** 的手術。

請現在立刻執行以下步驟，這才是真正的「部署」。做完這步，你把電腦砸了它都會繼續跑。

---

### 唯一解法：把它變成系統服務 (Systemd)

請重新連線進去 AWS，然後跟著我做。這次做完，我們才算完工。

#### 1. 確認你的程式路徑

先確認你的檔案在哪裡。輸入：

```bash
ls /home/ubuntu/my_monitor/risk_monitor.py

```

如果有看到檔案，代表路徑是對的。如果報錯，告訴我。

#### 2. 創建「服務設定檔」

我們要寫一張「命令狀」給 Linux 系統。

輸入：

```bash
sudo nano /etc/systemd/system/risk_monitor.service

```

#### 3. 貼上命令狀 (請完全複製)

在編輯器裡貼上以下內容：

```ini
[Unit]
Description=Abel Risk Monitor Service
After=network.target

[Service]
# 指定使用者
User=ubuntu
# 指定工作目錄
WorkingDirectory=/home/ubuntu/my_monitor
# 這裡最重要：告訴它用虛擬環境的 python 去跑你的腳本
ExecStart=/home/ubuntu/my_monitor/venv/bin/python risk_monitor.py

# 讓它永遠不死：如果掛了，10秒後自動復活
Restart=always
RestartSec=10

# 把 Log 存下來，之後你可以隨時查
StandardOutput=append:/home/ubuntu/my_monitor/service.log
StandardError=append:/home/ubuntu/my_monitor/service_error.log

[Install]
WantedBy=multi-user.target

```

* 按 `Ctrl + O` -> `Enter` 存檔。
* 按 `Ctrl + X` 退出。

#### 4. 蓋章生效 (啟動服務)

依序執行這三行指令：

```bash
# 1. 讓系統讀取新的命令狀
sudo systemctl daemon-reload

# 2. 正式啟動服務
sudo systemctl start risk_monitor

# 3. 設定開機自動啟動 (這步就是它為什麼不會斷的關鍵)
sudo systemctl enable risk_monitor

```

#### 5. 驗證它是否活著

輸入：

```bash
sudo systemctl status risk_monitor

```

* 如果你看到一顆 **綠燈 (Active: active running)**，代表成功了。
* 如果你看到紅燈，截圖給我。

---

### 如何證明我們沒在那邊瞎忙？

當你看到綠燈後，請做一個**殘酷測試**：

1. **直接把你的終端機視窗關掉 (Disconnect)。**
2. 甚至把你的筆電蓋上，去喝杯水。
3. 過 5 分鐘後，重新連線進去 AWS。
4. 輸入 `sudo systemctl status risk_monitor`。

你會發現，**它依然顯示 `Active: active (running)`，而且時間還在持續增加。**

這時候，你才真正完成了從「寫程式的學生」到「部署系統的工程師」的轉變。

**現在去做。做完截圖那個綠燈給我看。**