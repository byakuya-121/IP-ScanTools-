<div align="center">

# IP-ScanTools

### `NETWORK INTELLIGENCE / CONNECTION ANALYSIS`

**LINE OpenChat Connection Diagnostics**

接続情報の記録・分析を行う Python ベースのネットワーク診断ツール。

<br>

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white)
![Platform](https://img.shields.io/badge/Platform-Windows-0078D4?style=flat-square&logo=windows&logoColor=white)
![Version](https://img.shields.io/badge/Version-1.1-9ACD32?style=flat-square)
![Status](https://img.shields.io/badge/Status-Development-808080?style=flat-square)

<br>

`OBSERVE` &nbsp; / &nbsp; `ANALYZE` &nbsp; / &nbsp; `VERIFY`

---

</div>

## `01` — Overview

**IP-ScanTools** は、接続元のネットワーク情報やクライアント情報を記録し、ダッシュボード上で確認するための Python ツールです。

LINE オープンチャットのコミュニティ運営や、許可を得た環境での接続診断を想定しています。

### Core Capabilities

- **Connection Logging** — 接続イベントの記録
- **Network Analysis** — 利用可能なネットワーク情報の確認
- **GeoIP Lookup** — IPアドレスに基づく概算の地域・ISP情報
- **Client Metadata** — ブラウザや端末から取得できる情報の記録
- **Live Dashboard** — 接続イベントのリアルタイム表示
- **Request Classification** — 通常のアクセスと自動アクセスの判別補助
- **Answer Logging** — アンケート回答の記録

---

## `02` — Environment

| Requirement | Details |
|:--|:--|
| Operating System | Windows |
| Python | 3.10+ |
| Tunnel | Cloudflare Tunnel |
| Dependencies | Python標準ライブラリを使用する構成 |
| Dashboard | Local Web Dashboard |

※ 実際の依存関係や対応環境は、ソースコードの現状に合わせて確認してください。

---

## `03` — Setup

### Python

Python 3.10 以降をインストールしてください。

バージョンは以下で確認できます。

```powershell
python --version
```

### Cloudflare Tunnel

外部からの接続テストにトンネルを使用する場合は、公式配布元から `cloudflared.exe` を入手します。

**Official Repository**

https://github.com/cloudflare/cloudflared

取得した実行ファイルは、アプリケーションの設定に合わせて配置してください。

---

## `04` — Dashboard

ローカルダッシュボードでは、記録されたイベントを一覧形式で確認できます。

### Available Information

| Category | Description |
|:--|:--|
| Connection | 接続イベントの情報 |
| GeoIP | 利用可能な地域・ISP情報 |
| Client | ブラウザ・言語などのクライアント情報 |
| Validation | 接続情報の検証状態 |
| Answers | 送信されたアンケート回答 |
| Logs | 保存されたイベント履歴 |

取得できる情報は、ブラウザの制限、ネットワーク構成、プライバシー設定などによって異なります。

---

## `05` — Verification Status

v1.1 では、接続情報の検証状態を色分けして表示します。

| Status | Meaning |
|:--|:--|
| 🟢 `検証OK` | 比較対象のIP情報が一致 |
| 🟡 `IP相違` | 比較対象のIP情報が不一致 |
| 🔵 `v4/v6併用` | IPv4・IPv6の違いを考慮する必要がある状態 |
| ⚪ `未検証` | 検証に必要な情報を取得できていない状態 |
| 🔴 `BOT?` | 自動アクセスの可能性がある状態 |

これらの表示はあくまで判定の補助です。IP情報の一致だけで、アクセスした人物や端末の所有者を確定することはできません。

---

## `06` — Log Files

| File | Purpose |
|:--|:--|
| `hits.json` | ダッシュボード用の記録 |
| `hits.jsonl` | イベントの追記型ログ |
| `links.txt` | テスト用リンクなどの管理 |

ログにはネットワーク情報や端末情報が含まれる可能性があります。公開リポジトリへの追加を避け、アクセス権限と保存期間を適切に管理してください。

---

## `07` — Limitations

ネットワーク情報の分析には、以下の制約があります。

- IPアドレスだけでは個人を特定できません。
- GeoIPによる地域情報は概算であり、実際の所在地と一致するとは限りません。
- VPN・プロキシ・モバイル回線などにより、観測されるIPアドレスが変わる場合があります。
- ブラウザやネットワークの設定によって取得できる情報は異なります。
- リンクプレビューなどの自動アクセスが、利用者本人のアクセスと混在する場合があります。
- トンネルURLは設定や起動方法によって変わる場合があります。

---

## `08` — Privacy & Responsible Use

本ツールは、適切な権限と利用者の同意がある環境でのネットワーク診断を想定しています。

- 情報収集の目的と内容を事前に説明する。
- 必要最小限の情報のみを記録する。
- 取得した情報を第三者へ無断で公開しない。
- ログへのアクセスを制限し、不要になったデータを削除する。
- 個人の特定、嫌がらせ、無断追跡などの目的で使用しない。

**Security is not only about collecting information. It is also about protecting it.**

---

## `09` — Version History

### v1.1

- 接続情報の検証状態を色分け表示
- GeoIP情報のターミナル表示
- アンケート回答の記録機能
- ダッシュボードへの記録表示

※ 掲載内容は、実際のリリース内容と照合してから公開してください。

---

<div align="center">

### IP-ScanTools

`NETWORK ANALYSIS / CONNECTION DIAGNOSTICS`

**Built for responsible security research.**

</div>
