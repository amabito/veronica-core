---
title: "$12Kの週末：LLMエージェントを本番運用して初めてわかること"
published: false
tags: python, ai, llm, opensource
description: "LLMエージェントには、プロンプトもダッシュボードも解決できない構造的な問題がある。何が起きているのか、そしてコール前にリミットを強制する実行コンテナ層の話。"
cover_image:
canonical_url:
---

ある自律エージェントが週末に走り続けた。月曜日には47,000回のAPIコールを消費していた。

誰も予算上限を設定していなかった。リトライ回数も制限されていなかった。エージェントは一時的なAPIエラーに当たり、リトライし、また当たり、またリトライした——止まれと言うものが何もなかったので、60時間走り続けた。

これは特殊なケースではない。Simon Willisonがブログで記録している。1月のr/MachineLearningスレッドには800のアップボートがついた。金額はそれぞれ違う——$3K、$8K、$12K——でも形は毎回同じだ。

リトライループ＋予算上限なし＝際限のない課金。

厄介なのは、ほとんどのチームが間違った対策に手を伸ばすことだ。

---

## 間違った対策

まず思いつくのはオブザーバビリティだ。コストアラートを設定する。ダッシュボードを作る。OpenTelemetryを繋ぐ。

どれも正しい。でもそれは封じ込めじゃない。

コストアラートはコールが終わってから発火する。コールはすでにトークンを消費している。お金はもう使われている。完了した出来事への通知を受け取っているだけだ。

次に思いつくのはリトライライブラリだ——Tenacity、backoff。これらは一時的な失敗を扱う。ドルの上限という概念は持っていない。プロセスが途中でクラッシュして自動復旧すれば、リトライカウンタはゼロに戻る。

他に試されることもあるが、どれも不十分だ：

**サーキットブレーカー**：再起動をまたいで状態を保持しない。ブレーカーを落とし、プロセスが死に、自動復旧が走る——ブレーカーはなくなっている。

**プロバイダーの支出上限**：アカウント単位で、コールチェーン単位ではない。サブエージェントをまたいで伝播しない。プロバイダー側で強制されるので、コールとレスポンスの間にプロセスがクラッシュしても、こちら側に状態は残らない。

**手動のコスト追跡**：Agent AがAgent Bを生成し、BがCを生成するまで機能する。誰もバジェットを紐付けることを考えていなければ、そこで終わりだ。

ギャップは具体的だ：**コールが発生する前に、再起動をまたいで存続する形で、実行の境界を強制するものが存在しない。**

---

## 本質的な問題

LLMエージェントは確率的でコストを生み続けるコンポーネントでありながら、信頼性を求められるシステムの中に組み込まれている。

この矛盾は構造的だ。より良いプロンプトでも、より新しいモデルでも、より丁寧なオーケストレーションでも解消しない。

必要なのはコンテナ層だ——コール時点で以下を強制するもの：

- このチェーンは最大$X使える
- このチェーンは最大Nステップ実行できる
- 停止して再起動しても、この制限は残る
- サブエージェントを生成したら、そのコストは自分の制限に加算される

OSはこれをプロセスに対してやっている。CPU時間、メモリ、ファイルディスクリプタ——OSはプログラムが何をしているかは知らない。ただリソース契約を強制する。

LLMシステムにはこれに相当するものがまだない。

---

## 実際の実装

```python
from veronica_core.containment import ExecutionConfig, ExecutionContext

with ExecutionContext(config=ExecutionConfig(
    max_cost_usd=1.00,
    max_steps=50,
    max_retries_total=10,
    timeout_ms=30_000,
)) as ctx:
    decision = ctx.wrap_llm_call(fn=my_agent_step)
    # 上限を超えたら Decision.HALT を返す
    # HALT 時は fn は呼ばれない——ネットワークリクエストは発生しない
```

`wrap_llm_call` は `fn` を呼ぶ**前に**バジェットを確認する。上限に達していれば `Decision.HALT` を返し、ネットワークリクエストは発生しない。お金は使われない。

### マルチエージェントへのコスト伝播

より難しい問題はエージェントがエージェントを生成するケースだ：

```python
with ExecutionContext(config=ExecutionConfig(max_cost_usd=1.00)) as parent:
    with parent.spawn_child(max_cost_usd=0.50) as child:
        decision = child.wrap_llm_call(fn=sub_agent_step)
        # childのコストはparentの$1.00に加算される
        # 累計が$1.00を超えるとparentもHALTする
```

Agent Aは$1.00の上限を持つ。Aが生成したAgent Bは独自の$0.50のサブ上限を持つ。Bが$0.80使えば、その$0.80はAのバジェットにも加算される。誰も見ていないチェーンを通じて$1.00を超える前にAは止まる。

### kill -9 を生き延びる緊急停止

```python
from veronica_core import VeronicaIntegration
from veronica_core.state import VeronicaState

veronica = VeronicaIntegration()
veronica.state.transition(VeronicaState.SAFE_MODE, "operator halt")
# アトミックにディスクへ書き込む——tmpファイルのリネームパターン
# SIGKILL後も残る。自動復旧では解除されない。
# 再開するには明示的に .state.transition(VeronicaState.IDLE, ...) が必要
```

アトミック書き込み（tmpに書いてリネーム）によって、`kill -9` 後も状態が残る。自動復旧はSAFE_MODEを解除しない——意図的な設計だ。SAFE_MODEに入れたなら、再開の前に確認したいはずだ。

### ハードストップが望ましくないとき

止めるより段階的に劣化させたい場合：

```python
# バジェット80%: 安いモデルにダウングレード
# 85%: コンテキストをトリム開始
# 90%: コール間にレート制限を追加
# 100%: HALT
```

閾値とモデルのマッピングは設定可能。

### プロセスをまたぐ分散バジェット

```python
config = ExecutionConfig(max_cost_usd=10.00, redis_url="redis://localhost:6379")
# 全ワーカーがRedisのINCRBYFLOATで1つのバジェット上限を共有する
```

---

## 本番での数字

自律トレーディングシステムで稼働中：30日間継続、1,000+ ops/秒、260万回以上のオペレーション。

その間にクラッシュが12回（SIGTERM、SIGINT、OOMキル各種）。復旧率100%。データロストゼロ。

デストラクションテストは再現可能なので、言葉を信じる必要はない：

```bash
git clone https://github.com/amabito/veronica-core
python scripts/proof_runner.py
```

kill -9をまたぐSAFE_MODE永続化、バジェット上限強制、子エージェントへのコスト伝播——テストは通るかどうかだ。

---

## インストール

```bash
pip install veronica-core
```

コア部分は外部依存ゼロ。オプション：OTelエクスポートに `opentelemetry-sdk`、クロスプロセスのバジェット管理に `redis`。

LangChain、AutoGen、CrewAI、自作エージェントを問わず動作する。MITライセンス。

---

自分たちが内部で使うフレーミングはこうだ：LLMエージェントには、Unixプロセスが1970年代から持っているものが必要だ——リソース制限、構造化された終了処理、そして意味のあるHALT状態。

それはプロンプトではない。ダッシュボードでもない。

コンテナ層だ。

**GitHub**: [amabito/veronica-core](https://github.com/amabito/veronica-core)
