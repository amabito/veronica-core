# Veronica Evolution Roadmap -- v2.0 → v4.0

このドキュメントはveronica-coreの進化ロードマップである。v2.0完了を起点に、v3.0（基盤が賢い）、v4.0（基盤がエコシステム）へ向かう。各フェーズは独立してリリース可能な単位で区切られている。

**大前提**: Veronicaは「ランタイムポリシー制約エンジン」である。予算制御はポリシーの一種に過ぎない。制約の種類（経済、倫理、安全性、コンプライアンス）はポリシー層で差し替え可能であり、エンジン本体はポリシーの中身に依存しない。この設計思想を全ての実装で維持せよ。

**依存ゼロの原則は崩すな。** コア機能に外部依存を追加する場合は必ずoptionalにし、extras（`pip install veronica-core[redis]`等）で分離する。

---

## Phase A: 宣言的ポリシー層 (v2.1) [DONE]

### 目的
ShieldPipelineのポリシーをPythonコードではなくYAML/JSONで宣言的に定義可能にする。非エンジニア（コンプライアンス担当者、プロジェクトマネージャー）がポリシーを記述・変更できるようにする。

### 実装項目

#### A-1: PolicySchema定義
```yaml
# example: budget_policy.yaml
version: "1"
name: "project-alpha-budget"
rules:
  - type: token_budget
    params:
      max_tokens: 1_000_000
      period: monthly
      on_exceed: halt
  - type: cost_ceiling
    params:
      max_cost_usd: 500.00
      on_exceed: degrade
      degrade_to: "gpt-4o-mini"
  - type: rate_limit
    params:
      max_calls_per_minute: 60
      on_exceed: queue
  - type: circuit_breaker
    params:
      failure_threshold: 5
      reset_timeout_sec: 300
```

- Pydanticモデルで PolicySchema を定義
- 各ruleの `type` はレジストリパターンで拡張可能にする
- `on_exceed` のアクション: halt / degrade / queue / warn / custom
- バリデーション: 矛盾するルールの検出（例: halt と degrade が同じ条件に付いている）

#### A-2: PolicyLoader
```python
class PolicyLoader:
    def load(self, path: str | Path) -> ShieldPipeline: ...
    def load_from_string(self, content: str, format: str = "yaml") -> ShieldPipeline: ...
    def validate(self, path: str | Path) -> list[PolicyValidationError]: ...
```

- YAML / JSON 両対応
- `validate()` はload前のドライバリデーション（本番投入前チェック用）
- ファイル監視によるhot reload対応（オプション、watchdog不要、polling fallback）

#### A-3: PolicyRegistry
```python
class PolicyRegistry:
    def register_rule_type(self, name: str, factory: Callable) -> None: ...
    def get_rule_type(self, name: str) -> Callable: ...
```

- ビルトインrule types: token_budget, cost_ceiling, rate_limit, circuit_breaker, step_limit, time_limit
- カスタムrule typeの登録API
- **倫理ポリシーもここに登録可能**（例: `type: harm_score_threshold`）。ただしビルトインには含めない。エンジンは制約の中身に関知しない。

#### A-4: テスト
- [ ] YAML/JSONからShieldPipelineが正しく構築されるか
- [ ] 不正なYAML（未知のrule type、矛盾するルール）でバリデーションエラーが出るか
- [ ] hot reload: ファイル変更後にパイプラインが更新されるか
- [ ] PolicyRegistryにカスタムrule typeを登録して動作するか

#### 完了条件
- `veronica_core.policy.load("policy.yaml")` でShieldPipelineが返る
- ビルトイン6 rule typeが全てYAMLから構築可能
- ドキュメント: YAML policy format reference

---

## Phase B: 適応的ポリシー (v2.2) [DONE]

### 目的
静的閾値ではなく、実行時のメトリクスに基づいて動的に制約を調整する。予測ベースの予算制御を実現する。

### 実装項目

#### B-1: BurnRateEstimator
```python
class BurnRateEstimator:
    def record(self, cost: float, timestamp: float) -> None: ...
    def current_rate(self, window_sec: float = 3600) -> float: ...
    def time_to_exhaustion(self, remaining_budget: float) -> float | None: ...
    def projected_cost(self, horizon_sec: float) -> float: ...
```

- スライディングウィンドウベースのバーンレート計算
- 指数移動平均（EMA）で直近の傾向を重み付け
- `time_to_exhaustion()`: 現在のレートで残予算がゼロになるまでの秒数
- `projected_cost()`: 指定期間後の累積コスト予測

#### B-2: AdaptiveThresholdPolicy
```python
class AdaptiveThresholdPolicy:
    def __init__(self, burn_rate: BurnRateEstimator, config: AdaptiveConfig): ...
    def evaluate(self, ctx: ExecutionContext) -> Decision: ...
```

- AdaptiveConfig:
  - `warn_at_exhaustion_hours`: 残り時間がこの値を下回ったらWARN（デフォルト: 24h）
  - `degrade_at_exhaustion_hours`: DEGRADE（デフォルト: 6h）
  - `halt_at_exhaustion_hours`: HALT（デフォルト: 1h）
- 段階的エスカレーション: ALLOW → WARN → DEGRADE → HALT
- バーンレートが急上昇した場合の即座のDEGRADE（spike detection）

#### B-3: AnomalyDetector
```python
class AnomalyDetector:
    def record(self, metric_name: str, value: float) -> None: ...
    def is_anomalous(self, metric_name: str, value: float) -> bool: ...
```

- Z-scoreベースの異常検知（外部依存なし、NumPy不要）
- メトリクス: コスト/リクエスト、レイテンシ、エラー率
- ウォームアップ期間（最初のN回はFalse固定）
- ShieldPipelineのフックとして挿入可能

#### B-4: テスト
- [ ] BurnRateEstimatorが定常的なコスト流入で正確な予測を返すか
- [ ] バーストトラフィックでspike detectionが作動するか
- [ ] AdaptiveThresholdPolicyの段階的エスカレーション
- [ ] AnomalyDetectorのZ-score計算の正確性（既知の分布で検証）

#### 完了条件
- 「このペースだとあと6時間で予算を使い切る」を検知してdegradeできる
- テスト: 正常→バースト→回復のシナリオで正しくALLOW→DEGRADE→ALLOWに遷移

---

## Phase C: マルチテナント (v2.3) [DONE]

### 目的
複数プロジェクト/チーム/エージェントに対して階層的な予算プールとポリシーを管理する。

### 実装項目

#### C-1: TenantHierarchy
```python
@dataclass
class Tenant:
    id: str
    parent_id: str | None
    budget_pool: BudgetPool
    policy: ShieldPipeline | None  # None = inherit from parent

class TenantRegistry:
    def register(self, tenant: Tenant) -> None: ...
    def get(self, tenant_id: str) -> Tenant: ...
    def resolve_policy(self, tenant_id: str) -> ShieldPipeline: ...
    def get_effective_budget(self, tenant_id: str) -> float: ...
```

- 階層: Organization → Project → Team → Agent
- ポリシー継承: 子テナントのポリシーが未定義なら親から継承
- 予算分配: 親テナントの予算プールから子テナントにreserve/commit
- v2.0のRedisBudgetBackend（reserve/commit/rollback）をそのまま活用

#### C-2: BudgetPool
```python
class BudgetPool:
    def __init__(self, total: float, backend: BudgetBackend): ...
    def allocate(self, child_id: str, amount: float) -> bool: ...
    def release(self, child_id: str) -> float: ...  # returns released amount
    def usage(self) -> dict[str, float]: ...
```

- 親プールからの排他的割り当て（distributed reserve）
- 子プールが未使用分をrelease → 親プールに戻る
- オーバーコミット防止: allocate合計 <= total

#### C-3: テスト
- [ ] 3階層（org → project → agent）の予算分配と消費
- [ ] 子テナントの予算超過が親テナントに波及しないこと
- [ ] ポリシー継承: 子がNoneなら親のポリシーが適用される
- [ ] concurrent allocate/releaseでrace conditionが起きないこと（Redis backend）

#### 完了条件
- 「Project AlphaにはGPT-4o月額$500、Project BetaにはGPT-4o-mini月額$100」を1つのVeronicaインスタンスで管理できる
- 各プロジェクト内のエージェントはプロジェクト予算を共有消費

---

## Phase D: OTelフィードバックループ (v2.4) [DONE]

### 目的
OTelのメトリクスを入力としてポリシー判断に使う。観測 → 判断 → 制約の閉ループを形成する。

### 実装項目

#### D-1: OTelMetricsIngester
```python
class OTelMetricsIngester:
    def ingest_span(self, span: dict) -> None: ...
    def get_agent_metrics(self, agent_id: str) -> AgentMetrics: ...

@dataclass
class AgentMetrics:
    total_tokens: int
    total_cost: float
    avg_latency_ms: float
    error_rate: float
    last_active: float
```

- OTelスパン（AG2フォーマット対応）からメトリクスを抽出
- メモリ内集計（永続化はBudgetBackendに委任）
- AG2のOTelスパンタイプ: conversation, agent, llm, tool, code_execution

#### D-2: MetricsDrivenPolicy
```python
class MetricsDrivenPolicy:
    def __init__(self, ingester: OTelMetricsIngester, rules: list[MetricRule]): ...
    def evaluate(self, agent_id: str, ctx: ExecutionContext) -> Decision: ...

@dataclass
class MetricRule:
    metric: str          # "avg_latency_ms", "error_rate", etc.
    operator: str        # "gt", "lt", "gte", "lte"
    threshold: float
    action: Decision     # HALT, DEGRADE, WARN
```

- OTelメトリクスに基づくリアルタイムポリシー判断
- 「このエージェントのエラー率が30%を超えたらDEGRADE」
- 「平均レイテンシが5000msを超えたらモデルをダウングレード」
- YAMLからも定義可能（Phase Aと統合）

#### D-3: テスト
- [ ] OTelスパンJSONからAgentMetricsが正しく集計されるか
- [ ] MetricsDrivenPolicyがメトリクス変化に応じて正しいDecisionを返すか
- [ ] AG2 OTelフォーマットとの互換性

#### 完了条件
- AG2のOTelスパンを入力として、エージェントの健全性に基づく制約判断が動く
- mszeのOTel実装（PR #2309）と直接連携可能

---

## Phase E: A2A信頼境界 (v2.7) [DONE]

### 目的
A2A（Agent-to-Agent）プロトコルで外部エージェントが参加する場合の制約管理。未知のエージェントに対するデフォルト制約、信頼レベルに基づく予算割り当て。

### 実装項目

#### E-1: TrustLevel
```python
class TrustLevel(Enum):
    UNTRUSTED = "untrusted"     # 外部A2Aエージェント（初見）
    PROVISIONAL = "provisional" # 実績あり、制限付き
    TRUSTED = "trusted"         # 内部エージェント or 認証済み外部
    PRIVILEGED = "privileged"   # 管理者レベル

@dataclass
class AgentIdentity:
    agent_id: str
    origin: str             # "local", "a2a", "mcp"
    trust_level: TrustLevel
    metadata: dict          # A2Aカードの情報等
```

- A2Aエージェントカードから trust_level を判定
- デフォルト: 外部A2A = UNTRUSTED
- 昇格条件: N回の正常実行後にPROVISIONALへ（設定可能）

#### E-2: TrustBasedPolicyRouter
```python
class TrustBasedPolicyRouter:
    def __init__(self, policies: dict[TrustLevel, ShieldPipeline]): ...
    def route(self, identity: AgentIdentity) -> ShieldPipeline: ...
```

- TrustLevelごとに異なるShieldPipelineを適用
- UNTRUSTED: 厳しい予算制限 + レート制限 + step制限
- TRUSTED: 通常のプロジェクト予算
- PRIVILEGED: 制約なし（管理操作用）

#### E-3: A2ABudgetIsolation
- 外部エージェントの予算は完全に分離
- Phase CのTenantHierarchyと統合: 外部エージェント = 専用テナント
- 予算超過時: A2Aプロトコルでエラーレスポンスを返す

#### E-4: テスト
- [ ] 外部A2AエージェントにUNTRUSTEDポリシーが適用されるか
- [ ] trust_level昇格後にポリシーが緩和されるか
- [ ] 外部エージェントの予算超過が内部エージェントに影響しないか
- [ ] A2Aカード情報からAgentIdentityが正しく構築されるか

#### 完了条件
- StockMoltのような公開プラットフォームで、未知のエージェントが参加しても予算が制御される
- AG2のA2Aサポートと直接連携可能

---

## Phase F: ポリシーシミュレーション (v2.6.0) [DONE]

### 目的
ポリシーを本番投入する前に、過去の実行ログに対してドライランする。「このポリシーだったら何が起きていたか」のWhat-if分析。

### 実装項目

#### F-1: ExecutionLog
```python
@dataclass
class ExecutionLogEntry:
    timestamp: float
    agent_id: str
    action: str           # "llm_call", "tool_call", "reply"
    cost: float
    tokens: int
    latency_ms: float
    success: bool

class ExecutionLog:
    def load(self, path: str) -> list[ExecutionLogEntry]: ...
    def from_otel_export(self, spans: list[dict]) -> list[ExecutionLogEntry]: ...
```

#### F-2: PolicySimulator
```python
class PolicySimulator:
    def __init__(self, pipeline: ShieldPipeline): ...
    def simulate(self, log: list[ExecutionLogEntry]) -> SimulationReport: ...

@dataclass
class SimulationReport:
    total_entries: int
    halted_count: int
    degraded_count: int
    warned_count: int
    cost_saved_estimate: float
    timeline: list[SimulationEvent]  # 時系列のポリシー判断ログ
```

- 過去ログを時系列に再生し、各エントリでポリシー判断を実行
- 「このポリシーを適用していたら$X節約できた」のレポート生成
- ComplianceExporterのデータをそのまま入力にできる

#### F-3: テスト
- [ ] 既知のログに対してシミュレーション結果が期待通りか
- [ ] OTelエクスポートからのログ変換が正しいか
- [ ] cost_saved_estimateの計算精度

#### 完了条件
- `veronica simulate --policy new_policy.yaml --log last_month.json` でレポートが出る
- 「先月のログにこのポリシーを適用すると$340節約できた」が表示される

---

## Phase G: フェデレーション (v4.0)

### 目的
複数のVeronicaインスタンスが組織間で連携し、クロスオーガニゼーションのエージェント協業で予算を分担管理する。

### 実装項目

#### G-1: FederationProtocol
```python
class FederationNode:
    def __init__(self, node_id: str, backend: BudgetBackend): ...
    async def request_budget(self, remote_node: str, amount: float) -> BudgetGrant: ...
    async def report_usage(self, grant_id: str, used: float) -> None: ...
    async def revoke_grant(self, grant_id: str) -> None: ...

@dataclass
class BudgetGrant:
    grant_id: str
    grantor: str
    grantee: str
    amount: float
    expires_at: float
    policy_constraints: ShieldPipeline | None
```

- ノード間の予算交渉プロトコル
- 時限付きグラント（期限切れで自動revoke）
- グラント元のポリシー制約をグラント先に強制
- 暗号署名（PolicySignerV2を活用）でグラントの真正性を検証

#### G-2: FederationGateway
- HTTP/gRPCベースのノード間通信
- mTLSによる相互認証
- これはextras依存（`pip install veronica-core[federation]`）

### 注意
- Phase Gはv4.0であり、最も先の話。他の全Phaseが安定してから着手
- A2A信頼境界（Phase E）が前提条件
- 実需が確認されてから実装。投機的に作るな

---

## 実装順序と依存関係

```
v2.0 (完了)
  │
  ├── Phase A: 宣言的ポリシー (v2.1) [DONE]
  │
  ├── Phase B: 適応的ポリシー (v2.2) [DONE]
  │
  ├── Phase C: マルチテナント (v2.3) [DONE]
  │
  ├── Phase D: OTelフィードバック (v2.4) [DONE]
  │
  ├── Phase E: A2A信頼境界 (v2.7) [DONE]
  │
  ├── Phase F: ポリシーシミュレーション (v2.6) [DONE]
  │
  ├── v3.0: God Class Split + AdapterCapabilities + AuditChain [DONE]
  │
  ├── v3.0.1-3.0.3: Security Audit (3 rounds, 108 fixes) [DONE]
  │
  └── Phase G: フェデレーション (v4.0)
        ← 実需確認後
        ← 新ロードマップ: docs/ROADMAP.md 参照
```

**v3.1以降のロードマップは `docs/ROADMAP.md` に移行。**
本ドキュメントはPhase A-F の設計記録として保持する。

## 並行作業の指針

- Phase A と Phase B は独立。並行して進められる。
- Phase C は A/B と独立だが、v2.0のRedisBudgetBackendのテストが安定していることが前提。
- Phase D 以降は前のPhaseの完了を待つ。
- **AG2のPR待ち期間はPhase A/B/Cを進めるのに使え。**
- **TRNの開発とは完全に独立。両方並行で回せ。**

## 各Phaseの推定工数（Claude Code併用前提）

| Phase | 推定日数 | 主な作業 |
|-------|---------|---------|
| A: 宣言的ポリシー | 2-3日 | Pydanticスキーマ、YAML parser、PolicyLoader |
| B: 適応的ポリシー | 2-3日 | BurnRateEstimator、AnomalyDetector |
| C: マルチテナント | 3-4日 | TenantHierarchy、BudgetPool階層 |
| D: OTelフィードバック | 2-3日 | MetricsIngester、MetricsDrivenPolicy |
| E: A2A信頼境界 | 3-4日 | TrustLevel、PolicyRouter、BudgetIsolation |
| F: ポリシーシミュレーション | 2-3日 | ExecutionLog、PolicySimulator |
| G: フェデレーション | 5-7日 | FederationProtocol、Gateway、mTLS |

合計: 約20-27日（Claude Code併用、テスト込み）

## 絶対に守ること

1. **依存ゼロの原則。** 外部ライブラリが必要な機能はextrasに分離。
2. **全Phaseでテストを書いてから次に進め。** カバレッジ90%以上を維持。
3. **ポリシーの中身にエンジンを依存させるな。** BudgetEnforcerもHarmScoreCheckerも同じShieldPipelineで動く設計を崩すな。
4. **各Phaseは独立してリリース可能であること。** Phase Bが未完でもPhase Aだけでリリースできる。
5. **AG2のPR状況に関わらずVeronicaの開発は止めるな。**
