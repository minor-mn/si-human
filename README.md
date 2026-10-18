# si-human：AI人物とビデオ通話する PoC

人物画像 1 枚から、MacBook のカメラとマイクを使って話せる AI 人物を作ります。
話し終えてからアバターが話し始めるまで、およそ 1.5〜2.5 秒です。

## しくみ

| 役割 | 担当 |
|---|---|
| アバターの映像・音声認識・音声合成・映像配信 | [Anam](https://anam.ai)（Cara-4） |
| 会話の生成と人格の制御 | Claude か OpenAI（画面で切り替え可） |
| 上の 2 つの中継と会話履歴 | このリポジトリの Python サーバー（`localhost` で動く） |

1 回のやりとりは次の順に進みます。

1. マイクの音声を Anam が受け取り、話し終わりを検知して文字に起こす。
2. ブラウザが、文字起こしとカメラの静止画をこのサーバーに送る。
3. サーバーが LLM に問い合わせ、返ってくる応答を少しずつブラウザに流す。
4. ブラウザが応答を Anam に渡し、Anam が音声と口の動きを合わせた映像を返す。

応答は全文がそろうのを待たずに次へ渡すので、LLM が書き始めた時点でアバターが話し始めます。
GPU を使う処理はすべてクラウド側で行うので、手元の Mac の負担は軽く済みます。

## 必要なもの

- Python 3.11 以上と、Chrome（Safari でも可）
- Anam のアカウント
- LLM の API キー（Claude か OpenAI のどちらか一方でよい）
- 人物の画像 1 枚（条件は「2. 人物の画像を用意する」を参照）

---

## セットアップ手順

### 1. API キーを用意する

**Anam**

1. [Anam Lab](https://lab.anam.ai) でアカウントを作る。
2. https://lab.anam.ai/api-keys で「Create API key」を押し、表示されたキーをコピーする。

**キーが表示されるのは作成時の 1 回だけです。** すぐに手順 4 の `.env` に貼ってください。なくした場合は作り直します。

**LLM（どちらか一方）**

- Claude：[Claude Console](https://platform.claude.com) でキーを発行する。`ant auth login` でログイン済みなら、キーは不要です。
- OpenAI：[OpenAI Platform](https://platform.openai.com) でキーを発行する。

OpenAI のプロジェクトで使えるモデルを制限していると、そのモデルを呼んだときに 403（`does not have access to model`）になります。プロジェクトの制限を外したあとも、制限中に作ったキーには古い権限が残っていることがあります。その場合は、キーの権限を変えるか、キーを作り直してください。

### 2. 人物の画像を用意する

Anam はアップロードされた画像を審査し、条件に合わないものは拒否します。次の条件を満たす画像を用意してください。

- **一目で大人（18 歳以上）に見えること**：顔が幼く見えると、服装やポーズに問題がなくても年齢確認で弾かれます。猫耳などの小物や、幼く見せるメイクも、弾かれる原因になります。
- **服装・場所・ポーズに性的な要素がないこと**
- **実在の人物の写真や、有名人に似た顔は使わないこと**
- **正方形で 1152×1152px 以上**（JPEG / PNG / WebP、4.5MB 以下）
- **頭から胸の上あたりまで写っていて、周りに少し余白があること**：縦長・横長の画像や余白のない画像は、Anam が足りない部分を AI で描き足すので、仕上がりが落ちます。
- **手が写っていないこと**：顔や胸の前の手は、通話中に崩れることがあります。
- **背景がシンプルなこと**

AI で生成する場合のプロンプト例です。

> Photorealistic head-and-shoulders portrait of a 30-year-old Japanese professional woman, natural mature makeup, realistic skin texture, wearing a tailored business suit, calm smile, looking at the camera, plain softly blurred background, eye-level shot, square format

画像は `images/` フォルダに置きます（例：`images/miyu.webp`）。このフォルダの中の画像は Git の管理から外してあります。

審査は 1 分に 5 回、1 日に 100 回までで、弾かれた回も数えられます。同じ画像で何度も試すのは避けてください。

### 3. 依存パッケージを入れる

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

### 4. `.env` を作る

```bash
cp env.example .env
```

`.env` に次の値を入れます。

| 項目 | 書く値 |
|---|---|
| `ANAM_API_KEY` | 手順 1 の Anam のキー |
| `ANTHROPIC_API_KEY` か `OPENAI_API_KEY` | 使う LLM のキー |
| `LLM_PROVIDER` | 既定で使う LLM。OpenAI だけを使うなら `openai` |
| `ANAM_MAX_SESSION_SECONDS` | 1 回の通話の上限秒数。**プランの上限を超えると通話を開始できません。** 無料プランは `180`、Explorer は `600` |

モデルや思考量などの設定は、下の「設定の一覧」を参照してください。

### 5. 人物の設定を書く

```bash
cp persona.example.yaml persona.yaml
```

`persona.yaml` に、名前・年齢・性別・性格・話し方・経歴・最初の挨拶を書きます。`image:` には手順 2 の画像のパスを書きます。
`avatar_id`・`voice_id`・`appearance` は、次の手順で自動的に入ります。

### 6. アバターを作り、声を選ぶ

```bash
.venv/bin/python -m scripts.setup_persona persona.yaml
```

このスクリプトは、次の 3 つを順に行います。それぞれ終わった時点で `persona.yaml` に保存するので、途中で失敗しても、それまでの結果は残ります。

1. **アバターの作成**：画像を Anam にアップロードします。1〜2 分かかります。
2. **声の選択**：日本語の声の一覧が試聴 URL 付きで出るので、聞いてから番号を入力します。
3. **見た目の説明の生成**：LLM が画像を見て、外見の説明を `appearance` に書き込みます。システムプロンプトの「見た目」に使われます。内容は手で直してかまいません。

主なオプションです。

| オプション | 用途 |
|---|---|
| `--skip-avatar` | 作成済みのアバターを使い、作成と審査を飛ばす |
| `--provider openai` | 見た目の説明を、`.env` の設定とは別の LLM で生成する |
| `--voice-id <ID>` | 声を ID で直接指定する |

声を選び直すときは、`persona.yaml` の `voice_id:` を空にしてから、`--skip-avatar` を付けて再実行します。

声の選び方の目安です。

- 同じ声に複数のモデルがある場合、`eleven_v3_conversational` は抑揚が自然、`eleven_flash_v2_5` は応答が速いのが特徴です。
- `(ja)` が付いた Cartesia の声は性別が登録されていないので、試聴して選んでください。

#### Anam Lab の画面でアバターを作った場合

[Anam Lab](https://lab.anam.ai) の画面からアバターを作った場合は、その **アバターの ID** を `persona.yaml` の `avatar_id:` に書いてから、`--skip-avatar` を付けて手順 6 を実行します。

Lab の URL（`lab.anam.ai/build/…`）に出ている ID は **ペルソナの ID** で、アバターの ID とは別物です。これを使うと、通話の開始時に `is a persona ID` というエラーになります。

自分で作ったアバターの ID は、次のコマンドで一覧できます。

```bash
.venv/bin/python -c "from dotenv import load_dotenv; load_dotenv(); import httpx, os; r = httpx.get('https://api.anam.ai/v1/avatars', headers={'Authorization': 'Bearer ' + os.environ['ANAM_API_KEY']}, params={'perPage': 100}); r.raise_for_status(); [print(a['createdAt'], a['id'], a['displayName']) for a in r.json()['data'] if a.get('createdByOrganizationId')]"
```

プランによって置けるアバターの数が決まっています（無料プランは 1 つ）。不要なアバターは Lab から削除してください。

### 7. 起動する

ターミナルで動かす場合です。Ctrl+C で止まります。

```bash
.venv/bin/uvicorn app.server:app --port 8000
```

バックグラウンドで動かす場合は、こちらを使います。

```bash
scripts/server.sh start
```

| コマンド | 動き |
|---|---|
| `scripts/server.sh start` | バックグラウンドで起動する。起動直後に落ちた場合は、ログの末尾を表示する |
| `scripts/server.sh stop` | 停止する |
| `scripts/server.sh restart` | 再起動する。`.env` や `persona.yaml` を変えたときに使う |
| `scripts/server.sh status` | 起動しているかを表示する |
| `scripts/server.sh logs` | ログを流し続けて表示する。Ctrl+C で抜けてもサーバーは止まらない |

ログは `logs/server.log` に出ます。ポートは `PORT=8001 scripts/server.sh start` のように変えられます。

`.env` と `persona.yaml` は起動したときにしか読み込まないので、変えたら再起動してください。

### 8. 話す

1. ブラウザで http://localhost:8000 を開く。
2. 「LLM」のプルダウンで Claude か OpenAI を選ぶ。
3. 「接続準備」を押す。話題の取得・要約とコメントの保管が終わると、「接続する」が表示される。
4. 「接続する」を押し、マイクとカメラの使用を許可する。人物につながると、準備済みの話題を紹介する。

画面で操作できることです。

- **最初の挨拶**：アバターが `persona.yaml` の `greeting` で話しかけてきます。そのあとは普通に話しかけてください。
- **接続時の話題紹介**：過去24時間のITセキュリティ・AIの話題を件数制限なく準備します。最初の挨拶では最大4件だけを各20〜40字程度の一文で紹介し、残りの件数も伝えます。長い前置きや出典名の読み上げは省きます。画面には準備した全話題・出典リンク・取得基準時刻を表示し、「ほかの話題は？」にも答えられます。同じ会話の再接続では紹介を繰り返しません。
- **スレッドのコメント・返信**：準備した全話題の公開コメントやメーリングリストの返信を接続準備で取得し、会話ごとに保管します。接続時には読み上げず、「その件のコメントは？」「その報告への返信は？」「みんなの反応は？」などと尋ねられた後に、その実データを使って回答します。通常の質問ではコメント・返信データをLLMに渡しません。
- **割り込み**：アバターが話している途中でも、話し始めれば割り込めます。
- **カメラ映像をAIに見せる**：オンにすると、話すたびにカメラの静止画を 1 枚、LLM に送ります。
- **自分の映像を表示**：右下の小窓の表示・非表示を切り替えます。AI に静止画を送るかどうかとは別の設定です。
- **遅延**：話し終えてから LLM の応答の最初の文字が届くまでの秒数を、使った LLM の名前と一緒に表示します。
- **続きから再開**：通話が切れても、「続きから接続」で会話の続きから話せます。LLM を変えてから再開すると、同じ会話を別の LLM で続けられます。「新しい話題を準備」を押すと、新しい話題で別の会話を準備します。会話履歴はサーバーのメモリに保持するので、サーバーを再起動すると消えます。

話題の取得元は海外版[Slashdotの公式Atomフィード](https://rss.slashdot.org/Slashdot/slashdotMainatom)と[Full Disclosureの公式RSS](https://seclists.org/rss/fulldisclosure.rss)です。受信したフィードに含まれる、準備時点から過去24時間の投稿を全件保持します。以前の候補30件・保存話題4件の上限はありません。片方に新着がない場合や取得に失敗した場合も、もう片方の話題を使います。

選択中のLLMは投稿を10件ずつ・同時に最大2組処理し、ITセキュリティ・AIに該当するすべての話題を日本語に要約します。同じ出来事の重複だけを避け、重要度が低いという理由では除外しません。要約対象外の投稿も取得元・掲載日時・タイトル・フィードの説明文をJSONに残します。説明文はアーカイブでは600字で切りません。要約用・会話用の資料には600字までの抜粋を使います。

Full DisclosureはRSSの掲載日時を基準にし、`Re:` で始まる返信もJSONの取得データに残しますが、独立した話題としては要約しません。返信の取得データを要約時のLLMや画面のAPIに混ぜず、投稿者の脆弱性報告とベンダーの確認・修正も区別して扱います。

冒頭ではフィードの見出し・説明文から、何に何が起きたかだけを最大60字の一文で伝えます。長すぎる文・出典名・書きかけの文が生成された場合は準備中に一度だけ修正を求め、改善しなければ準備失敗とします。未紹介の話題も質問に使えます。対象の話題がなかった場合も取得した投稿はJSONに保存します。以前に保存した準備データから、当時除外した投稿を復元することはできないため、新しい保存方式にするには準備し直してください。

Slashdotのコメントは選ばれたスレッドの公開ページから、本文・評価・返信先・リンクを抽出します。取得できた範囲から評価順に最大12件、各本文は最大1200文字を保管します。Full Disclosureでは、元の投稿ページの「Current thread」にある返信関係から、その投稿への返信だけをスレッド順に最大12件・各1200文字で取得します。元の報告を返信として混ぜず、引用文や定型フッターも除外します。メーリングリストの返信には投票の評価値はありません。取得先は各サイトの公開投稿URLに限定し、リダイレクトは追わず、同時取得は最大4接続です。

省略された本文や一部だけ取得したスレッドも区別して記録し、スレッド全体の総意や賛否の割合は推測しないよう指示しています。コメント・返信の取得は接続準備で済ませ、質問された後は保管済みの資料を会話に追加します。件数が多くて取得時間を超えた場合も、取得済みのスレッドは保存し、未完了の話題には取得できなかった状態を残します。返信がない場合、取得失敗、本文を確認できなかった場合はその状態を記録します。取得の失敗や一部の返信しか取得できなかった場合は準備完了時に知らせます。

フィードは30分間キャッシュします。[Slashdotの取得間隔の案内](https://slashdot.org/faq/feeds.shtml)に合わせたもので、キャッシュを使う場合も準備時点の過去24時間で日時を絞り直します。取得・要約の待ち時間と要約分のLLM利用料は「接続準備」で発生します。話題を準備できなかった場合は接続ボタンを表示せず、やり直せるようにします。準備だけでは人物への接続やカメラ・マイクの取得を始めません。準備完了後にLLMを変更した場合は、準備をやり直します。

「接続する」は保存済みの準備データを使い、話題の取得・要約やコメントの収集をやり直さずに人物へ接続します。ニュース機能を止めるには `.env` に `NEWS_ON_CONNECT=false` を設定して再起動してください。その場合も「接続準備」→「接続する」の操作で、通常の挨拶から通話できます。

準備完了時には、`data/preparations/<準備ID>.json` に取得した投稿全件（`news.articles`）・対象話題全件の要約（`news.items`）・短い冒頭紹介（`news.text`）・出典・取得したコメント・取得日時・使用したLLMの種類を保存します。JSONは日本語をそのまま読める形式で、保存先は `PREPARATION_DIR` で変更できます。保存先のディレクトリは自動作成し、中身はGit管理から除外します。APIキー・Anamのトークン・カメラ画像・会話履歴はこのJSONに含めません。

未接続の準備IDをブラウザに記憶するため、ページを開き直しても保存済みのJSONから準備完了の状態を復元できます。サーバー再起動後も再取得・再要約は行いません。コメントは復元後も質問されるまで会話用のLLMに渡しません。接続に使ったJSONは `used: true` に更新して記録として残し、同じ準備IDによる重複接続は防ぎます。壊れたJSONは消さずに残して、新しい接続準備を案内します。

---

## Anam のプランと上限

| プラン | 月額 | 含まれる時間 | 1 回の通話の上限 |
|---|---|---|---|
| Free | $0 | 30 分 | 3 分 |
| Starter | $12 | 50 分 | 5 分 |
| Explorer | $49 | 250 分（超過分は 1 分 $0.14） | 10 分 |
| Growth | $299 | 2,000 分（超過分は 1 分 $0.12） | なし |

2026 年 10 月時点の [Anam の料金ページ](https://anam.ai/pricing) の内容です。使わなかった時間は翌月に繰り越されません。

### 月額の目安（1 日 1 時間、月 30 時間使う場合）

| 構成 | 月額の目安 |
|---|---|
| Explorer ＋ 超過分（約 $266）＋ LLM | 約 4.0〜4.8 万円 |
| Growth（$299）＋ LLM | 約 4.5〜5.2 万円 |

LLM の料金は、モデルによって月数百円〜約 7,000 円です。Claude Opus 5.5 がいちばん高く、Haiku 5.5 や gpt-6-luna がいちばん安くなります。会話の履歴はプロンプトキャッシュに乗るので、入力の料金は大きく下がります。
1 ドル＝150 円で計算した概算です。実際のトークン数はサーバーのログで、Anam の利用時間は Lab の画面で確認できます。

## うまくいかないとき

| 症状・エラー | 原因と対処 |
|---|---|
| アバター作成で `PROHIBITED_CONTENT` | 画像の内容が審査で拒否された。手順 2 の条件に合う画像を新しく用意する |
| アバター作成で `couldn't confidently verify that the person in the image is an adult` | 人物が若く見える。手順 2 の「一目で大人に見えること」を参照 |
| 通話開始で `Invalid request to start session (… is a persona ID …)` | `avatar_id` にペルソナの ID が入っている。手順 6 のコマンドでアバターの ID を調べる |
| 通話開始で `Invalid request to start session`（その他） | `ANAM_MAX_SESSION_SECONDS` がプランの上限を超えている可能性がある。括弧内の理由も確認する |
| アバターが「ちょっと調子が悪いみたいです」と答える | LLM の呼び出しに失敗している。サーバーのログに原因が 1 行で出る |
| `Could not resolve authentication method` | Claude のキーがない。`ANTHROPIC_API_KEY` を入れるか、`LLM_PROVIDER=openai` にする |
| `does not have access to model` | OpenAI のプロジェクトかキーで、そのモデルが許可されていない。手順 1 を参照するか、`OPENAI_MODEL` を使えるモデルに変える |
| カメラ・マイクが使えない | `localhost` 以外の `http://` で開いている。下の「サーバーに置く場合」を参照 |

OpenAI のキーで使えるモデルは、次のコマンドで確認できます。

```bash
.venv/bin/python -c "from dotenv import load_dotenv; load_dotenv(); import openai; [print(m.id) for m in sorted(openai.OpenAI().models.list(), key=lambda m: m.id) if m.id.startswith(('gpt', 'chat', 'o'))]"
```

## サーバーに置く場合

このアプリは、手元の Mac で 1 人が使う前提で作っています。サーバーに置いてスマホなどから使う場合は、次の 2 つが **必須** です。

- **HTTPS にする**：ブラウザは、HTTPS のページか `localhost` でしかカメラとマイクを使わせません。Caddy や Cloudflare Tunnel などで HTTPS にしてください。
- **アクセスを制限する**：このアプリにはログイン機能がありません。そのまま公開すると、URL を知っている誰もが通話でき、あなたの Anam と LLM の料金が使われます。Basic 認証や Cloudflare Access などで、アクセスできる人を限ってください。

---

## 設定の一覧（`.env`）

| 環境変数 | 既定値 | 説明 |
|---|---|---|
| `ANAM_API_KEY` | なし | Anam の API キー |
| `ANTHROPIC_API_KEY` | なし | Claude の API キー（`ant auth login` 済みなら不要） |
| `OPENAI_API_KEY` | なし | OpenAI の API キー |
| `LLM_PROVIDER` | `claude` | 既定で使う LLM（`claude` か `openai`）。画面のプルダウンで通話ごとに変えられる |
| `CLAUDE_MODEL` | `claude-opus-5-5` | 応答の速さを優先するなら `claude-haiku-5-5` |
| `CLAUDE_EFFORT` | `low` | 上げると考えが深くなるが、応答までの時間が延びる |
| `OPENAI_MODEL` | `gpt-6.1-sol` | 応答の速さを優先するなら `gpt-6-luna` |
| `OPENAI_REASONING_EFFORT` | `low` | `gpt-6-luna` なら `none` にして考える時間をなくせる |
| `ANAM_MAX_SESSION_SECONDS` | `600` | 1 回の通話の上限秒数。プランの上限以下にする（無料プランは `180`） |
| `NEWS_ON_CONNECT` | `true` | 接続準備で、Slashdot・Full Disclosureの過去24時間の話題とコメント・返信を用意する。`false` で無効 |
| `PREPARATION_DIR` | `data/preparations` | 準備データのJSON保存先。相対パスはプロジェクトルートから解決する |
| `PERSONA_FILE` | `persona.yaml` | 人物設定のファイル。複数の人物を切り替えるときに使う |

## ファイル構成

| パス | 内容 |
|---|---|
| `app/server.py` | FastAPI のサーバー。接続準備（`/api/prepare`）、人物への接続（`/api/session`）、応答のストリーミング（`/api/chat`） |
| `app/conversation.py` | 会話履歴の管理、割り込み、LLM がエラーになったときの処理 |
| `app/news.py` | 過去24時間のニュース案内、取得失敗時の処理、会話に引き継ぐニュース情報 |
| `app/comments.py` | 公開コメント・返信の取得・保管と、質問されたときに渡す資料の作成 |
| `app/fulldisclosure.py` | Full Disclosureの公開投稿の本文・返信関係の解析 |
| `app/preparation_store.py` | 準備データのJSON保存と読み込み。書き終えてからファイルを置き換える |
| `app/llm.py` | Claude と OpenAI の切り替え。履歴を各 API の形式に変換し、応答をストリーミングで受け取る |
| `app/persona.py` | `persona.yaml` の読み込みと、システムプロンプトの組み立て |
| `app/anam.py` | Anam REST API の呼び出し（アバター作成・声の一覧・トークン発行） |
| `scripts/setup_persona.py` | アバターの作成、声の選択、見た目の説明の生成 |
| `scripts/server.sh` | バックグラウンドでの起動・停止・再起動 |
| `static/` | ブラウザ側（Anam JS SDK、カメラの撮影、応答の受け渡し） |
| `persona.example.yaml` | 人物設定のひな形 |
| `env.example` | `.env` のひな形 |
| `images/` | 人物の画像を置く場所（中身は Git の管理外） |
| `logs/`・`run/` | ログと PID ファイル（中身は Git の管理外） |
| `data/preparations/` | 話題・コメントの準備データのJSON（自動作成、Git管理外） |
