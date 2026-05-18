# WiFi 定位比賽 — 系統 V1

## 依賴
```bash
sudo apt install tshark python3-pip
pip3 install numpy scipy pyyaml
# tshark 要允許非 root 用戶 (或全程 sudo):
sudo dpkg-reconfigure wireshark-common  # 選 Yes
sudo usermod -aG wireshark $USER
```

## 檔案結構
```
wifi_pos/
├── config.yaml          # 全域設定
├── scan_channels.py     # 賽前: 掃出 4 AP 的 BSSID/channel
├── calibrate.py         # 賽前: 校準 path loss
├── collect_rssi.py      # 比賽中: 每點跑一次
├── solve.py             # 賽後: 解算 7 個 (x,y,z)
└── data/                # CSV 儲放處 (執行時自動建立)
```

## 賽前流程

### 1. 確認介面名
```bash
iw dev
# 找到 AWUS036AXML 對應的介面 (應該是 wlan1)
# 更新 config.yaml 的 collection.interface
```

### 2. 通道偵察 (到場後)
```bash
sudo python3 scan_channels.py --iface wlan1 --duration 60 --ssid-prefix infra_
# 確認結果無誤後:
mv config.yaml.updated config.yaml
```

### 3. Path Loss 校準
找場域內 3-4 個位置已知的點,在每個點跑量測:
```bash
# 預先設好 monitor mode
sudo ip link set wlan1 down
sudo iw dev wlan1 set type monitor
sudo ip link set wlan1 up

# 每個校準點各跑一次
sudo python3 collect_rssi.py --point cal_1 --round 1 --duration 30
sudo python3 collect_rssi.py --point cal_2 --round 1 --duration 30
sudo python3 collect_rssi.py --point cal_3 --round 1 --duration 30
sudo python3 collect_rssi.py --point cal_4 --round 1 --duration 30
```

建立 `calibration.yaml`:
```yaml
points:
  cal_1: { position: [2.0, 2.0, 1.0], csv_glob: "data/cal_1_*.csv" }
  cal_2: { position: [12.0, 2.0, 1.0], csv_glob: "data/cal_2_*.csv" }
  cal_3: { position: [12.0, 7.0, 1.0], csv_glob: "data/cal_3_*.csv" }
  cal_4: { position: [2.0, 7.0, 1.0], csv_glob: "data/cal_4_*.csv" }
```

執行擬合:
```bash
python3 calibrate.py --calib-file calibration.yaml
# 預覽無誤後:
python3 calibrate.py --calib-file calibration.yaml --write
```

## 比賽流程

### 預備 (比賽前 30 秒)
```bash
# 一次性把 monitor mode 設好,之後 14 次都重用,省每次的 setup time
sudo ip link set wlan1 down
sudo iw dev wlan1 set type monitor
sudo ip link set wlan1 up
```

### 14 次量測 (每點 20 秒)
建議用一個簡單的包裝 script:
```bash
for pt in P1 P2 P3 P4 P5 P6 P7; do
  echo "準備就位點 $pt round 1, 按 enter 開始..."
  read
  sudo python3 collect_rssi.py --point $pt --round 1
done
# 第二輪同理
```

## 賽後解算
```bash
python3 solve.py --data-dir data/ --verbose --output result.csv
cat result.csv
```

## 風險清單

| 風險 | 偵測方式 | 對應 |
|------|---------|------|
| 某個 AP 收不到 | collect_rssi 結束時顯示樣本 0 | 重做該輪 / 換量測位置 |
| 樣本數太少 (< 5) | 同上 | 同上 |
| 校準擬合 RMSE 大 | calibrate.py 警告 | 加更多校準點 / 該 AP 用預設值 |
| 解算殘差大 | solve.py --verbose 看殘差 | 用 ensemble 擇優 |
| 兩輪結果發散 | solve.py 報 divergence | 自動擇優 (殘差小的) |
