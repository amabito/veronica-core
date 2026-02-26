# Migration Guide

## v0.12.x → v1.0

### ShieldPipeline on_error() default change
- **Before**: on_error() returned ALLOW (fail-open)
- **After**: on_error() returns HALT (fail-closed)
- **Migration**: Pass `on_error_policy=Decision.ALLOW` to ShieldPipeline() to restore old behavior
- **Rationale**: A security library should fail-closed by default

### timeout_ms deprecation
- **Before**: @veronica_guard(timeout_ms=5000)
- **After**: timeout_ms is accepted but ignored with DeprecationWarning
- **Migration**: Use ExecutionContext timeout mechanism instead
- **Removal**: Scheduled for v2.0

### AIcontainer → AIContainer
- **Before**: AIcontainer (lowercase 'c')
- **After**: AIContainer (PascalCase)
- **Migration**: Old name works with DeprecationWarning. Update imports.
