# subscope

開発系サブスク(Claude・Codex・GitHub)のクオータを、どれだけ使えているかを測る CLI。

- どのプロジェクト・どのワークフローがクオータを食っているかを棚卸しする
- 逆に、窓ごとにどれだけ余らせたまま期限を迎えたかを見る

## 何を読むか

| 出どころ | 読むもの | 分かること |
| --- | --- | --- |
| Claude Code(手元) | `~/.claude/projects/**/*.jsonl`(`CLAUDE_CONFIG_DIR` に従う) | リクエストごとのトークン、作業ディレクトリ、モデル、サブエージェントか、制限で断られた回数 |
| Claude Code(ステータスライン) | ステータスラインに渡る JSON の `rate_limits` | 5 時間・7 日の窓の消費率 |
| Claude Code Action | artifact `claude-usage-*`(shiroinock/workflows の Claude のジョブが OpenTelemetry の出力を残す) | リポジトリ・ワークフローごとのトークンと費用の目安 |
| Codex | `~/.codex/sessions/**/*.jsonl`(`CODEX_HOME` に従う) | リクエストごとのトークンと、primary・secondary の窓の消費率 |
| GitHub | 課金 API `/users/{user}/settings/billing/usage`(`gh` 経由) | Actions の分・保存容量などの明細と金額 |

集めたものは SQLite(`$XDG_DATA_HOME/subscope/subscope.db`、既定は `~/.local/share/subscope/subscope.db`)に貯める。ログは消えたり書き換わったりするので、こまめに `collect` しておくと長い期間を見られる。

## 使い方

```bash
nix run github:shiroinock/subscope -- report --collect
```

```bash
subscope collect                 # すべて集める
subscope collect --only claude   # Claude のログだけ
subscope report --days 7 --top 5
```

GitHub の課金 API には `gh` のトークンに `user` スコープが要る。

```bash
gh auth refresh -h github.com -s user
```

### 報告の読み方

トークンの量は「入力換算」にそろえて比べる。キャッシュの読み込みや出力は、普通の入力とクオータの減り方が違うので、API の単価の比で重みを付けている。

- Claude:入力 1、出力 5、キャッシュ読み込み 0.1、キャッシュ作成(5 分)1.25、キャッシュ作成(1 時間)2
- Codex:キャッシュに無い入力 1、キャッシュ済みの入力 0.1、出力 8

クオータの実際の減り方は公開されていないので、あくまで比べるための目安。消費率そのものは、ステータスライン(Claude)とログ(Codex)が示す値をそのまま使う。

## Claude の消費率を記録する

Claude Code はステータスラインのコマンドに JSON を標準入力で渡し、Pro・Max のサブスクではそこに `rate_limits` が入る。`subscope record-statusline` はそれを読んで記録する(何も出力せず、失敗しても 0 で終わる)。いまのステータスラインのコマンドの先頭で入力を分けて渡す。

```bash
input=$(cat); printf '%s' "$input" | subscope record-statusline &
# このあとは、これまでどおり "$input" から表示を作る
```

1 つの窓につき 1 分に 1 回だけ記録する。

## 開発

```bash
nix develop
python -m unittest discover -s tests
nix flake check
```
