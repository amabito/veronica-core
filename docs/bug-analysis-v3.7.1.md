# veronica-core v3.7.1 Bug Analysis Report

F.R.I.D.A.Y. 3-body audit で発見された10件のバグに対する根本原因分析。
発覚元: 全件コードレビュー (F.R.I.D.A.Y. 3-body review loop)。CI/本番での発覚ではない。

---

## B1: MCP stats KeyError (DoS限界到達後)

### 1. 事象整理

- **何が起きたか**: `_STATS_WARN_LIMIT=10000` を超える一意ツール名が投入されると、`_ensure_stats()` がエントリ作成をスキップする。その後 `self._stats[tool_name]` を直接キーアクセスするため `KeyError` が発生
- **期待される挙動**: stats エントリが存在しない場合、stats更新をスキップしてツール呼び出し自体は正常に続行
- **実際の挙動**: `KeyError` 例外でツール呼び出し全体が失敗
- **差分**: `self._stats[tool_name]` (直接アクセス) vs `self._stats.get(tool_name)` (安全アクセス)

### 2. テストで見つからなかった直接原因

- **条件を通るテストが存在しなかった**: `TestToolNameCardinalityWarning` の最大試行数は100件。`_STATS_WARN_LIMIT=10000` に到達するテストケースが皆無
- モック化の問題ではない。テストは実オブジェクトを使用していた
- 仕様は正しく理解されていた (DoS防止のための上限)。しかし「上限到達後のコードパス」の検証が完全に欠落

### 3. 根本原因の分類

1. **ケース設計不足** -- DoS上限到達後のコードパスが未検証
2. **境界値/例外系不足** -- 10000件目と10001件目の境界テストなし
3. **結合点の検証不足** -- `_ensure_stats()` の「スキップ」と後続の `self._stats[tool_name]` の結合が未検証

### 4. 事前に見つける方法

- **追加すべきテスト**: 単体テスト
- **具体的テストケース**: `test_tool_call_succeeds_after_stats_limit_reached`
- **入力条件**: `_STATS_WARN_LIMIT` をモンキーパッチで小さい値 (例: 5) に設定し、6件目のツール呼び出し
- **期待値**: `result.success is True` (stats はドロップされるがツール呼び出しは成功)
- **実行層**: CI / pre-merge
- **監視**: `KeyError` の例外ログで早期検知可能だった

### 5. 最小の再発防止策

| 対策 | なぜ効くか | 実装コスト | 運用コスト | 効果 | 優先度 |
|------|----------|-----------|-----------|------|--------|
| dict直接アクセスを `.get()` に統一するlintルール | KeyError源を根絶 | 低 (ruff custom rule) | 低 | 高 | P0 |
| `_STATS_WARN_LIMIT` 境界テスト追加 | 上限前後の動作を保証 | 低 | なし | 高 | P0 |

### 6. テスト改善案

| 欠けていた観点 | テスト名 | レベル | 検証内容 | 期待値 | 捕捉理由 |
|---------------|---------|--------|---------|--------|---------|
| DoS上限後の正常動作 | `test_tool_call_succeeds_after_stats_limit` | 単体 | limit超過後のwrap_tool_call | success=True | KeyError発生を直接検出 |
| 上限境界 | `test_stats_entry_created_at_limit_minus_one` | 単体 | limit-1件目のstats存在確認 | stats is not None | 境界での正常動作を保証 |

### 7. レビュー観点

- **違和感**: `_ensure_stats()` が「作成しない」ケースがあるのに、後続コードが `self._stats[tool_name]` を無条件アクセスしている
- **入れるべきコメント**: 「`_ensure_stats` がスキップした場合、この行で KeyError になる。`.get()` に変更すべき」

### 8. 最終結論

- **最大要因**: DoS防止の上限到達後のコードパスがテスト設計から完全に漏れていた
- **一番効く対策**: `self._stats[tool_name]` を `.get()` に統一 + 境界テスト追加
- **チームルール**: 「ガード/制限を設ける場合、制限到達後のコードパスも必ずテストする」

---

## B2: DegradeDirective merge で max() を使っていた

### 1. 事象整理

- **何が起きたか**: 2つの DegradeDirective をマージする際、`max()` を使用していたため「緩い方」が勝つ
- **期待される挙動**: 制限のマージは「厳しい方が勝つ」= `min()` (ただし 0=no-limit)
- **実際の挙動**: `max(100, 200) = 200` -- ポリシーAの100トークン制限がポリシーBの200で上書きされる
- **差分**: `max()` vs `_merge_limit()` (0=no-limit対応の `min()`)

### 2. テストで見つからなかった直接原因

- **仕様誤解により、誤った期待値でテストしていた**: `test_int_fields_max` が `max()` の結果を正しい期待値として検証していた。テスト名自体が `_max` であり、仕様の誤解がテスト設計に入り込んでいた
- tautological test: 実装をそのまま鏡像テストしていた

### 3. 根本原因の分類

1. **仕様理解の誤り** -- 「制限値のマージはmax()」という誤った前提でテストが書かれた
2. **ケース設計不足** -- 「ポリシーAが設けた制限をポリシーBが緩められてはならない」というセキュリティ不変条件のテストがない

### 4. 事前に見つける方法

- **追加すべきテスト**: プロパティベーステスト
- **具体的テストケース**: `test_merge_never_weakens_either_directive`
- **入力条件**: 任意の2つの DegradeDirective (hypothesis生成)
- **期待値**: `merged.max_packet_tokens <= max(d1.max_packet_tokens, d2.max_packet_tokens)` (0=no-limit除外)
- **実行層**: CI / pre-merge
- **監視**: ポリシー評価ログで「マージ後の制限値が入力より緩い」ケースをアラート

### 5. 最小の再発防止策

| 対策 | なぜ効くか | 実装コスト | 運用コスト | 効果 | 優先度 |
|------|----------|-----------|-----------|------|--------|
| セキュリティ不変条件テスト: 「マージは制限を緩めない」 | 仕様レベルの検証 | 低 | なし | 極高 | P0 |
| テスト名に期待される動作を明記 (max -> stricter_wins) | 仕様誤解を表面化 | 極低 | なし | 中 | P1 |

### 6. テスト改善案

| 欠けていた観点 | テスト名 | レベル | 検証内容 | 期待値 | 捕捉理由 |
|---------------|---------|--------|---------|--------|---------|
| セキュリティ不変条件 | `test_merge_never_weakens_limits` | プロパティベース | 任意の2 directive merge | merged <= max(d1, d2) | max()だと違反を即検出 |
| 0=no-limit semantics | `test_zero_and_positive_returns_positive` | 単体 | merge(0, 256) | 256 | 0が「制限なし」として機能する |

### 7. レビュー観点

- **違和感**: セキュリティ制限のマージに `max()` を使っている -- 「max = 緩い方が勝つ」のは直感に反する
- **入れるべきコメント**: 「制限のマージで max() は危険。ポリシーAの制限をポリシーBが突破できてしまう。min() + 0=no-limit が正しいはず」

### 8. 最終結論

- **最大要因**: テストが実装の鏡像になっており、仕様の正しさを独立に検証していなかった
- **一番効く対策**: セキュリティ不変条件のプロパティベーステスト
- **チームルール**: 「制限/ガード値のマージには不変条件テストを必ず書く: マージ後は入力より厳しいか等しい」

---

## B3: AG2 agent identity で name 文字列キー (同名衝突)

### 1. 事象整理

- **何が起きたか**: `agent.name` を辞書キーにしていたため、同名の別インスタンスが同一視された
- **期待される挙動**: 各 agent インスタンスが独立した CircuitBreaker を持つ
- **実際の挙動**: `StubAgent("alice")` を2つ登録すると、2つ目が1つ目の breaker を上書き
- **差分**: `name` 文字列キー vs UUID (`_veronica_agent_key`) によるインスタンス識別

### 2. テストで見つからなかった直接原因

- **条件を通るテストが存在しなかった**: 同名の異なるインスタンスを同時に登録するテストケースが皆無
- `test_remove_allows_readd` は同一オブジェクトの remove+readd であり、name ベースキーでも正常動作する
- `test_idempotent_second_call_is_noop` は同一オブジェクトの二重登録であり、同名別インスタンスではない

### 3. 根本原因の分類

1. **ケース設計不足** -- 同名別インスタンスのシナリオが欠落
2. **実装変更に対する回帰テスト不足** -- 識別子の選択 (name vs id vs UUID) に対するテストがない
3. **結合点の検証不足** -- add + add + 独立動作の3ステップ検証がない

### 4. 事前に見つける方法

- **追加すべきテスト**: 単体テスト
- **具体的テストケース**: `test_same_name_agents_get_separate_breakers`
- **入力条件**: `StubAgent("shared")` を2つ作成し、両方 `add_to_agent`
- **期待値**: `breaker_a is not breaker_b`
- **実行層**: CI / pre-merge

### 5. 最小の再発防止策

| 対策 | なぜ効くか | 実装コスト | 運用コスト | 効果 | 優先度 |
|------|----------|-----------|-----------|------|--------|
| 同名別インスタンスのテストを必須化 | 識別子の衝突を検出 | 低 | なし | 高 | P0 |
| dict キーにユーザー可視値を使わない規約 | 衝突の根本原因を排除 | 極低 (規約のみ) | なし | 高 | P1 |

### 6. テスト改善案

| 欠けていた観点 | テスト名 | レベル | 検証内容 | 期待値 | 捕捉理由 |
|---------------|---------|--------|---------|--------|---------|
| 同名衝突 | `test_same_name_agents_get_separate_breakers` | 単体 | 同名2 agent のbreaker独立性 | `breaker_a is not breaker_b` | name キーだと同一視される |
| remove+readd独立性 | `test_readd_gets_fresh_breaker` | 単体 | remove後readd のbreaker新規性 | `key_1 != key_2` | 古い状態を継承しない |

### 7. レビュー観点

- **違和感**: `agent.name` をdict キーにしている -- ユーザーが同名agentを作成する可能性がある
- **入れるべきコメント**: 「name は一意性を保証できない。id() や UUID をキーにすべき」

### 8. 最終結論

- **最大要因**: 「エージェント名は一意」という暗黙の前提がテスト設計に入り込んでいた
- **一番効く対策**: 同名別インスタンスのテストを標準化
- **チームルール**: 「オブジェクト識別にユーザー可視の名前を使わない。UUID またはオブジェクト固有の不変キーを使う」

---

## B4: BudgetEnforcer.spend() のゼロバジェットガード欠落

### 1. 事象整理

- **何が起きたか**: `limit_usd=0.0` のとき `spend(0.0)` が `True` (支出許可) を返す
- **期待される挙動**: ゼロバジェットでは全ての支出が拒否される
- **実際の挙動**: `projected=0.0 > 0.0` は `False` なので `True` が返る
- **差分**: `check()` にはゼロバジェットガードがあったが、`spend()` にはなかった

### 2. テストで見つからなかった直接原因

- **条件は通っていたが、アサーション対象が異なっていた**: `TestBudgetZeroLimit` は `check()` のみテスト。`spend()` を `limit_usd=0.0` で呼ぶテストが一切存在しなかった
- `check()` と `spend()` は同一クラスの異なるメソッドだが、テスト設計で片方のみカバーされていた

### 3. 根本原因の分類

1. **結合点の検証不足** -- `check()` と `spend()` が同一境界条件で整合することの検証が欠落
2. **境界値/例外系不足** -- `limit_usd=0.0` + `spend()` の組み合わせが未検証
3. **ケース設計不足** -- 「同一クラスの全公開メソッドに同一境界値を適用する」という系統的テスト設計がない

### 4. 事前に見つける方法

- **追加すべきテスト**: 単体テスト
- **具体的テストケース**: `test_spend_zero_on_zero_budget_returns_false`
- **入力条件**: `BudgetEnforcer(limit_usd=0.0)` に `spend(0.0)` 呼び出し
- **期待値**: `result is False`
- **実行層**: CI / pre-merge

### 5. 最小の再発防止策

| 対策 | なぜ効くか | 実装コスト | 運用コスト | 効果 | 優先度 |
|------|----------|-----------|-----------|------|--------|
| 境界値テストを全公開メソッドに適用する規約 | check/spend の非対称を検出 | 低 | なし | 高 | P0 |
| プロパティベーステスト: check()==deny ならば spend()==False | 不変条件の検証 | 中 | なし | 極高 | P0 |

### 6. テスト改善案

| 欠けていた観点 | テスト名 | レベル | 検証内容 | 期待値 | 捕捉理由 |
|---------------|---------|--------|---------|--------|---------|
| spend+zero-budget | `test_spend_zero_on_zero_budget_returns_false` | 単体 | spend(0.0) on limit=0 | False | 0.0 > 0.0 の偽判定を検出 |
| check/spend一貫性 | `test_check_deny_implies_spend_deny` | 単体 | check==deny時のspend | False | メソッド間の非対称を検出 |

### 7. レビュー観点

- **違和感**: `check()` にゼロバジェットガードがあるのに `spend()` にない -- API の非対称性
- **入れるべきコメント**: 「`check()` にある `limit_usd == 0.0` ガードが `spend()` にない。整合性を確認すべき」

### 8. 最終結論

- **最大要因**: 同一境界条件を同一クラスの全メソッドに適用するテスト設計パターンの欠如
- **一番効く対策**: 「check() が deny なら spend() も deny」という不変条件テスト
- **チームルール**: 「同一クラスの query メソッド (check) と mutation メソッド (spend) に同一境界値を必ず適用する」

---

## B5: Zero-token masking (`or` が 0 を falsy 扱い)

### 1. 事象整理

- **何が起きたか**: `usage.get("prompt_tokens") or usage.get("input_tokens")` で `prompt_tokens=0` が falsy として扱われ、`input_tokens` にフォールバックされる
- **期待される挙動**: `prompt_tokens=0` は有効な値。`None` のときのみフォールバック
- **実際の挙動**: `0 or fallback` = `fallback` (Python の truthy 評価)
- **差分**: `x or y` vs `x if x is not None else y`

### 2. テストで見つからなかった直接原因

- **条件を通るテストが存在しなかった**: 全テストが非ゼロのトークン数 (100, 120, 1000等) のみ使用。`prompt_tokens=0` を入力するテストが皆無
- Python の `or` 演算子の falsy 評価 (`0`, `""`, `[]`, `None` を同一視) は周知だが、テスト設計で「0 は有効値」という観点が抜けていた

### 3. 根本原因の分類

1. **境界値/例外系不足** -- `0` は int の境界値だがテストされていない
2. **ケース設計不足** -- `or` 演算子の falsy 評価を型バリエーションテストでカバーしていない

### 4. 事前に見つける方法

- **追加すべきテスト**: 単体テスト
- **具体的テストケース**: `test_zero_prompt_tokens_not_masked_by_input_tokens`
- **入力条件**: `usage = {"prompt_tokens": 0, "input_tokens": 500}`
- **期待値**: `prompt_tokens == 0` (500 にフォールバックしない)
- **実行層**: CI / pre-merge

### 5. 最小の再発防止策

| 対策 | なぜ効くか | 実装コスト | 運用コスト | 効果 | 優先度 |
|------|----------|-----------|-----------|------|--------|
| `x or y` を `x if x is not None else y` に統一するlintルール | 0 の falsy 扱いを根絶 | 低 (ruff/semgrep) | 低 | 極高 | P0 |
| 数値フィールドのゼロ値テストを必須化 | 境界値テストの標準化 | 低 | なし | 高 | P0 |

### 6. テスト改善案

| 欠けていた観点 | テスト名 | レベル | 検証内容 | 期待値 | 捕捉理由 |
|---------------|---------|--------|---------|--------|---------|
| ゼロ値マスキング | `test_zero_prompt_tokens_not_masked` | 単体 | prompt_tokens=0 + input_tokens=500 | prompt==0 | or の falsy を検出 |
| None フォールバック | `test_none_prompt_tokens_falls_back` | 単体 | prompt_tokens=None + input_tokens=500 | prompt==500 | 正当なフォールバック |

### 7. レビュー観点

- **違和感**: `x or y` でデフォルト値を設定 -- x が 0 の場合を考慮しているか?
- **入れるべきコメント**: 「`or` は `0` も falsy 扱い。`prompt_tokens=0` が有効値なら `is not None` チェックが必要」

### 8. 最終結論

- **最大要因**: Python の `or` 演算子の falsy セマンティクスと、テストが非ゼロ値のみで書かれていたこと
- **一番効く対策**: `x or y` パターンを lint で警告 + ゼロ値テスト必須化
- **チームルール**: 「dict.get() のフォールバックに `or` を使わない。`is not None` で明示チェック」

---

## B6: Git policy non-determinism + GIT_PUSH_APPROVAL 過大許可

### 1. 事象整理

- **何が起きたか**: (a) `next(iter(set))` で set 反復順序が不定。(b) `GIT_PUSH_APPROVAL` が push 以外の denied subcmd も許可
- **期待される挙動**: (a) 同一入力で常に同一結果。(b) push のみ許可、workflow/release/tag は常に拒否
- **実際の挙動**: (a) 実行ごとに異なる subcmd がマッチ。(b) `GIT_PUSH_APPROVAL` + `git workflow run` が ALLOW
- **差分**: `next(iter(set))` vs `min(set)` / capability チェック強化

### 2. テストで見つからなかった直接原因

- **非決定的要因**: set の反復順序は実行ごとに異なるが、テストは単一 denied subcmd のみで実行。複数 denied subcmd が同時に存在するケースがなく、`next(iter({single_element}))` は常に決定的
- **条件を通るテストが存在しなかった**: `GIT_PUSH_APPROVAL` + push 以外の denied subcmd の組み合わせテストが欠落 (クロスプロダクトシナリオ)

### 3. 根本原因の分類

1. **非決定性バグへの対策不足** -- set 反復の非決定性に対するテストがない
2. **ケース設計不足** -- capability と denied subcmd のクロスプロダクトテストが欠落
3. **境界値/例外系不足** -- 複数 denied subcmd が同時存在するケース

### 4. 事前に見つける方法

- **追加すべきテスト**: 単体 + プロパティベース
- **具体的テストケース**: `test_push_approval_does_not_bypass_workflow_deny`
- **入力条件**: `caps=[GIT_PUSH_APPROVAL]`, `args=["push", "workflow", "run"]`
- **期待値**: `verdict == "DENY"` (workflow は push approval で通過してはならない)
- **実行層**: CI / pre-merge

### 5. 最小の再発防止策

| 対策 | なぜ効くか | 実装コスト | 運用コスト | 効果 | 優先度 |
|------|----------|-----------|-----------|------|--------|
| `next(iter(set))` を lint で警告 | 非決定性の根絶 | 低 (semgrep rule) | 低 | 高 | P0 |
| capability x denied subcmd のクロスプロダクトテスト | 過大許可の検出 | 中 | なし | 極高 | P0 |

### 6. テスト改善案

| 欠けていた観点 | テスト名 | レベル | 検証内容 | 期待値 | 捕捉理由 |
|---------------|---------|--------|---------|--------|---------|
| capability bypass | `test_push_approval_does_not_bypass_workflow` | 単体 | PUSH_APPROVAL + workflow | DENY | 過大許可を検出 |
| 決定性 | `test_multiple_denied_subcmds_deterministic` | 単体 | 同一入力100回 | 全て同一結果 | set 反復の非決定性を検出 |

### 7. レビュー観点

- **違和感**: `next(iter(set))` -- set の反復順序は保証されない
- **入れるべきコメント**: 「`set` の反復順序は非決定的。`sorted()` か `min()` を使うべき。また `GIT_PUSH_APPROVAL` が push 以外にも効いていないか確認」

### 8. 最終結論

- **最大要因**: 単一 denied subcmd のみのテストで、クロスプロダクトシナリオが欠落
- **一番効く対策**: capability x denied subcmd のクロスプロダクトテスト
- **チームルール**: 「capability/permission チェックは、許可対象以外のリソースに効かないことを明示テストする」

---

## B7: CircuitBreaker record_success() OPEN状態 + HALF_OPEN stuck

### 1. 事象整理

- **何が起きたか**: (a) OPEN 状態で stale callback から `record_success()` が呼ばれると CLOSED に遷移。(b) `_maybe_half_open_locked()` が `_half_open_in_flight` をリセットしないため、OPEN->HALF_OPEN->OPEN->HALF_OPEN の二周回で永久stuck
- **期待される挙動**: (a) OPEN で record_success は no-op。(b) HALF_OPEN 遷移時に in_flight カウンタがリセット
- **実際の挙動**: (a) OPEN -> CLOSED に不正遷移。(b) 二周回目の HALF_OPEN で全 check() が deny
- **差分**: OPEN ガード追加 + HALF_OPEN 遷移時の `_half_open_in_flight = 0` リセット

### 2. テストで見つからなかった直接原因

- **条件を通るテストが存在しなかった**: OPEN 状態で `record_success()` を呼ぶシナリオが皆無。テストは常に check() -> record_success() の正常フローのみ
- **状態遷移の二周回テストが欠落**: HALF_OPEN -> (failure) -> OPEN -> (timeout) -> HALF_OPEN という遷移を経るテストがない
- 単体テストでは見えず、遅延コールバックという非同期シナリオでしか発生しない

### 3. 根本原因の分類

1. **状態遷移の考慮漏れ** -- OPEN 状態での record_success() が未考慮
2. **ケース設計不足** -- 状態遷移の二周回テストが欠落
3. **非決定性バグへの対策不足** -- stale callback (非同期タイミング) のシナリオ

### 4. 事前に見つける方法

- **追加すべきテスト**: 単体 + 並行実行
- **具体的テストケース**: `test_stale_success_in_open_is_noop`
- **入力条件**: CB を OPEN にした後、`record_success()` を呼ぶ
- **期待値**: `state == CircuitState.OPEN` (変化しない)
- **実行層**: CI / pre-merge

### 5. 最小の再発防止策

| 対策 | なぜ効くか | 実装コスト | 運用コスト | 効果 | 優先度 |
|------|----------|-----------|-----------|------|--------|
| 状態遷移テーブルに基づく全遷移テスト | 全状態x全イベントをカバー | 中 | なし | 極高 | P0 |
| 「各状態で全メソッドを呼ぶ」テストパターン | 未考慮の状態xメソッド組み合わせを検出 | 中 | なし | 極高 | P0 |

### 6. テスト改善案

| 欠けていた観点 | テスト名 | レベル | 検証内容 | 期待値 | 捕捉理由 |
|---------------|---------|--------|---------|--------|---------|
| OPEN + success | `test_stale_success_in_open_is_noop` | 単体 | OPEN状態でrecord_success | state==OPEN | 不正遷移を検出 |
| 二周回遷移 | `test_half_open_reentry_after_failure` | 単体 | HO->fail->OPEN->timeout->HO->check | allowed==True | in_flight stuck を検出 |

### 7. レビュー観点

- **違和感**: `record_success()` に state ガードがない -- OPEN 状態で呼ばれたらどうなるか?
- **入れるべきコメント**: 「record_success() は HALF_OPEN 前提だが、OPEN で呼ばれた場合の動作を確認。stale callback のケースを考慮すべき」

### 8. 最終結論

- **最大要因**: 状態遷移マシンの全状態x全イベントの組み合わせテストが欠如
- **一番効く対策**: 状態遷移テーブルベースのテスト生成
- **チームルール**: 「状態遷移マシンは全状態x全イベントのテストマトリクスを書く。未定義遷移は明示的に no-op をテストする」

---

## B8: AgentStepGuard が bool を max_steps として受け入れる

### 1. 事象整理

- **何が起きたか**: `isinstance(True, int)` が Python で `True` を返すため、`max_steps=True` (=1) や `max_steps=False` (=0) が検証なく通過
- **期待される挙動**: `bool` 型は拒否し `TypeError` を発生
- **実際の挙動**: `max_steps=True` が `max_steps=1` として受理
- **差分**: `__post_init__` に `isinstance(x, bool)` チェック追加

### 2. テストで見つからなかった直接原因

- **条件を通るテストが存在しなかった**: コンストラクタのバリデーションテスト自体が存在しなかった
- Python の `bool` is subclass of `int` という言語仕様の落とし穴への認識不足
- `dataclass` が型アノテーションを実行時に強制しないという前提がテスト設計に入り込んでいた

### 3. 根本原因の分類

1. **境界値/例外系不足** -- コンストラクタへの不正入力テストなし
2. **仕様理解の誤り** -- `int` 型アノテーションが `bool` を排除すると誤認

### 4. 事前に見つける方法

- **追加すべきテスト**: 単体テスト
- **具体的テストケース**: `test_bool_true_raises_type_error`
- **入力条件**: `AgentStepGuard(max_steps=True)`
- **期待値**: `TypeError`
- **実行層**: CI / pre-merge

### 5. 最小の再発防止策

| 対策 | なぜ効くか | 実装コスト | 運用コスト | 効果 | 優先度 |
|------|----------|-----------|-----------|------|--------|
| int パラメータの bool 排除チェックをユーティリティ化 | 全 dataclass で再利用 | 低 | なし | 高 | P1 |
| dataclass の `__post_init__` テストを標準化 | バリデーション漏れの検出 | 低 | なし | 高 | P1 |

### 6. テスト改善案

| 欠けていた観点 | テスト名 | レベル | 検証内容 | 期待値 | 捕捉理由 |
|---------------|---------|--------|---------|--------|---------|
| bool排除 | `test_bool_true_raises_type_error` | 単体 | max_steps=True | TypeError | bool subclass of int を検出 |
| 負数排除 | `test_negative_raises_value_error` | 単体 | max_steps=-1 | ValueError | 境界値チェック |

### 7. レビュー観点

- **違和感**: `max_steps: int` -- Python の dataclass は型を強制しない。`bool` も通る
- **入れるべきコメント**: 「dataclass の型アノテーションは実行時に検証されない。`__post_init__` でバリデーションが必要」

### 8. 最終結論

- **最大要因**: Python の `bool` is `int` サブクラスという言語仕様と、dataclass が型を強制しないことへの認識不足
- **一番効く対策**: dataclass の `__post_init__` テストの標準化
- **チームルール**: 「dataclass の int フィールドは `__post_init__` で bool 排除チェックを入れる」

---

## S1: MCP エラーメッセージでの credential leak

### 1. 事象整理

- **何が起きたか**: `error=f"{type(exc).__name__}: {exc}"` が例外クラス名とメッセージを外部に公開。`CredentialExpiredError: token=sk-xxxx...` のような情報が漏洩しうる
- **期待される挙動**: ユーザー向けエラーは generic。詳細は内部ログのみ
- **実際の挙動**: 例外のフルメッセージが `MCPToolResult.error` に含まれる
- **差分**: `f"{type(exc).__name__}: {exc}"` vs `"tool call failed"`

### 2. テストで見つからなかった直接原因

- **仕様誤解により、誤った期待値でテストしていた**: 10個以上のテストが `"RuntimeError" in result.error` を正しい仕様として検証。テストが情報漏洩をバグではなく「デバッグ可能性のための仕様」として定義していた
- セキュリティ視点のテスト (「エラーメッセージに機密情報が含まれないこと」) が皆無

### 3. 根本原因の分類

1. **仕様理解の誤り** -- エラーメッセージの情報量をデバッグ優先で設計し、セキュリティ視点が欠落
2. **ケース設計不足** -- 機密情報を含む例外メッセージのテストなし
3. **監視/観測不足** -- エラーメッセージの内容を監視するセキュリティテストなし

### 4. 事前に見つける方法

- **追加すべきテスト**: セキュリティテスト
- **具体的テストケース**: `test_error_message_does_not_leak_exception_details`
- **入力条件**: `CredentialError("token=sk-xxxx")` を発生させるモック
- **期待値**: `"sk-xxxx" not in result.error` かつ `"Credential" not in result.error`
- **実行層**: CI / pre-merge + security-audit nightly

### 5. 最小の再発防止策

| 対策 | なぜ効くか | 実装コスト | 運用コスト | 効果 | 優先度 |
|------|----------|-----------|-----------|------|--------|
| 外部公開エラーメッセージに例外詳細を含めない規約 | 情報漏洩の根本原因を排除 | 極低 | なし | 極高 | P0 |
| `MCPToolResult.error` に `exc` が含まれないことのテスト | 回帰防止 | 低 | なし | 高 | P0 |

### 6. テスト改善案

| 欠けていた観点 | テスト名 | レベル | 検証内容 | 期待値 | 捕捉理由 |
|---------------|---------|--------|---------|--------|---------|
| 情報漏洩防止 | `test_error_does_not_contain_exception_type` | 単体 | error フィールド | "RuntimeError" not in error | 例外型名の漏洩を検出 |
| credential漏洩 | `test_error_does_not_leak_credential` | 単体 | CredentialError("token=sk-xxx") | "sk-xxx" not in error | credential の漏洩を検出 |

### 7. レビュー観点

- **違和感**: `f"{type(exc).__name__}: {exc}"` -- 例外メッセージをそのまま外部公開している
- **入れるべきコメント**: 「例外メッセージには credential が含まれうる。generic エラーに変更すべき」

### 8. 最終結論

- **最大要因**: テストがバグを仕様として積極的に検証していた (tautological test の変種)
- **一番効く対策**: 外部公開エラーメッセージに例外詳細を含めない規約
- **チームルール**: 「外部公開される文字列に `str(exc)` や `type(exc).__name__` を含めない。内部ログのみに出力する」

---

## S2: 内部 debug ログから例外メッセージが消えた (S1 修正の回帰)

### 1. 事象整理

- **何が起きたか**: S1 修正時に `error` フィールドを generic にしたが、一部の内部 debug ログからも `exc` のメッセージが脱落した
- **期待される挙動**: 外部エラーは generic、内部ログには例外の型名とメッセージが残る
- **実際の挙動**: 内部ログに `type(exc).__name__` のみで `str(exc)` が欠落 (一部パス)
- **差分**: `logger.debug("... %s", type(exc).__name__)` vs `logger.debug("... %s: %s", type(exc).__name__, exc)`

### 2. テストで見つからなかった直接原因

- **サイドチャネル (ログ出力) のテストが完全に欠落**: テストは戻り値 (`MCPToolResult`) のみ検証。`caplog` を使ったログ内容の検証テストが皆無
- S1 修正が「外部エラー変更」と「内部ログ維持」の2つの変更を含んでいたが、後者の検証がなかった

### 3. 根本原因の分類

1. **監視/観測不足** -- ログ出力の内容をテストしていない
2. **実装変更に対する回帰テスト不足** -- S1 修正時に内部ログの回帰テストを追加しなかった

### 4. 事前に見つける方法

- **追加すべきテスト**: 単体テスト (caplog)
- **具体的テストケース**: `test_debug_log_contains_exception_message`
- **入力条件**: `RuntimeError("connection refused")` を発生させるモック
- **期待値**: `"connection refused" in caplog.text`
- **実行層**: CI / pre-merge

### 5. 最小の再発防止策

| 対策 | なぜ効くか | 実装コスト | 運用コスト | 効果 | 優先度 |
|------|----------|-----------|-----------|------|--------|
| エラーパスのログ内容テスト追加 | 診断情報の回帰防止 | 低 | なし | 高 | P1 |
| 「外部エラー変更時は内部ログのテストも追加」ルール | 変更の副作用をカバー | 極低 | なし | 中 | P1 |

### 6. テスト改善案

| 欠けていた観点 | テスト名 | レベル | 検証内容 | 期待値 | 捕捉理由 |
|---------------|---------|--------|---------|--------|---------|
| ログ内容 | `test_debug_log_contains_exc_message` | 単体 (caplog) | debug ログの内容 | exc メッセージ含む | ログからの情報脱落を検出 |

### 7. レビュー観点

- **違和感**: S1 修正で `error` を generic にしたなら、情報はどこに残るのか? -> ログ -> ログの検証は?
- **入れるべきコメント**: 「外部エラーから情報を削除した。内部ログに残す設計なら、ログ内容のテストを追加すべき」

### 8. 最終結論

- **最大要因**: ログ出力がテストのスコープ外だった
- **一番効く対策**: エラーパスの `caplog` テスト追加
- **チームルール**: 「外部エラーメッセージを変更する際、診断情報の移動先 (ログ) のテストも同時に追加する」

---

## 横断的分析: 共通パターン

### テストで見つからなかったバグの分類

| パターン | 該当バグ | 根本原因 |
|---------|---------|---------|
| **テストが実装の鏡像** (tautological) | B2, S1 | テストが実装を「正しい」と前提し、仕様を独立検証していない |
| **境界値の組み合わせ欠落** | B1, B4, B5, B8 | ゼロ、上限、falsy 値が系統的にテストされていない |
| **状態遷移の網羅不足** | B7 | 全状態x全イベントのテストマトリクスがない |
| **クロスプロダクト欠落** | B3, B6 | 複数パラメータの組み合わせテストがない |
| **サイドチャネル未検証** | S2 | 戻り値のみテスト、ログ出力は未検証 |

### 今後のチームルール (優先度順)

1. **P0**: 「制限/ガード値のマージは不変条件テストを書く: マージ後は入力より厳しいか等しい」
2. **P0**: 「同一クラスの query/mutation メソッドに同一境界値を適用する」
3. **P0**: 「ガードを設ける場合、ガード到達後のコードパスもテストする」
4. **P0**: 「外部公開文字列に `str(exc)` を含めない」
5. **P0**: 「dict.get() のフォールバックに `or` を使わない。`is not None` で明示チェック」
6. **P1**: 「状態遷移マシンは全状態x全イベントのテストマトリクスを書く」
7. **P1**: 「capability テストはクロスプロダクト: 許可対象以外に効かないことを検証」
8. **P1**: 「外部エラー変更時は内部ログのテストも同時追加」

---

## 今回追加すべきテスト3本 (サンプルコード)

### 1. 不変条件テスト: マージは制限を緩めない (B2 対策)

```python
import pytest
from hypothesis import given, strategies as st
from veronica_core.memory.types import DegradeDirective
from veronica_core.memory.governor import _merge_directives


@given(
    a_tokens=st.integers(min_value=0, max_value=10000),
    b_tokens=st.integers(min_value=0, max_value=10000),
)
def test_merge_never_weakens_packet_token_limit(a_tokens: int, b_tokens: int) -> None:
    """Merged limit must be <= max(inputs) -- merge never relaxes restrictions.

    0 means 'no limit', so it is excluded from the comparison.
    """
    d1 = DegradeDirective(max_packet_tokens=a_tokens)
    d2 = DegradeDirective(max_packet_tokens=b_tokens)
    merged = _merge_directives(d1, d2)
    assert merged is not None

    if a_tokens == 0 and b_tokens == 0:
        assert merged.max_packet_tokens == 0  # both unlimited -> unlimited
    elif a_tokens == 0:
        assert merged.max_packet_tokens == b_tokens
    elif b_tokens == 0:
        assert merged.max_packet_tokens == a_tokens
    else:
        assert merged.max_packet_tokens == min(a_tokens, b_tokens)
```

### 2. query/mutation 一貫性テスト: check deny => spend deny (B4 対策)

```python
from veronica_core.budget import BudgetEnforcer
from veronica_core.runtime_policy import PolicyContext


@pytest.mark.parametrize("amount", [0.0, 0.001, 1.0, 100.0])
def test_check_deny_implies_spend_deny(amount: float) -> None:
    """If check() denies an amount, spend() must also deny it.

    This invariant must hold for all budget states including zero-budget.
    """
    enforcer = BudgetEnforcer(limit_usd=0.0)
    check_result = enforcer.check(PolicyContext(cost_usd=amount))
    spend_result = enforcer.spend(amount)
    assert not check_result.allowed
    assert spend_result is False
```

### 3. 状態遷移マトリクステスト: 全状態で record_success (B7 対策)

```python
import time
from veronica_core.circuit_breaker import CircuitBreaker, CircuitState
from veronica_core.runtime_policy import PolicyContext


class TestRecordSuccessInEveryState:
    """record_success() must be safe to call in any state."""

    def test_success_in_closed_stays_closed(self) -> None:
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=0.1)
        assert cb.state == CircuitState.CLOSED
        cb.record_success()
        assert cb.state == CircuitState.CLOSED

    def test_success_in_open_is_noop(self) -> None:
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=60)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        cb.record_success()  # stale callback
        assert cb.state == CircuitState.OPEN  # must NOT transition

    def test_success_in_half_open_closes(self) -> None:
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0.01)
        cb.record_failure()
        cb.record_failure()
        time.sleep(0.02)
        cb.check(PolicyContext())  # triggers HALF_OPEN
        assert cb.state == CircuitState.HALF_OPEN
        cb.record_success()
        assert cb.state == CircuitState.CLOSED
```
