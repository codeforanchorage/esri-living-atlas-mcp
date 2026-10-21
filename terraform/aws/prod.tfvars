lambda_name = "living-atlas-mcp-prod"
stage_name  = "prod"
aws_region  = "us-west-2"
config_file = "config.yaml"
lambda_memory  = 512
# Overridden by aws.lambda_timeout in config.yaml (main.tf reads config
# first); kept equal to it so the two cannot drift. 28s sits under API
# Gateway's hard 29s integration timeout -- see config.yaml.
lambda_timeout = 28
api_quota_limit = 3000
api_rate_limit  = 5
api_burst_limit = 10

# DNS for codeforanchorage.org is managed externally (DreamHost): the ACM
# validation CNAME and the final CNAME to the API Gateway regional domain
# are created there — see the README deploy steps.
custom_domain = "living-atlas.codeforanchorage.org"

# Cap concurrent Lambda executions. Cost and blast-radius protection if
# WAF is bypassed via distributed sources. Conversational MCP traffic does
# not need horizontal scale; raise if legitimate users start getting throttled.
lambda_reserved_concurrency = 10

# WAF per-IP rate limit (rolling 5-minute window). The MCP tools are
# conversational, so 1 rps sustained per IP (~300/5min) is plenty for
# real users and tight enough to slow scrapers and denial-of-wallet probes.
# It also keeps this server a polite client of ArcGIS Online upstream.
waf_rate_limit_per_5min = 300

# Use the fleet-wide WAF instead of a dedicated ACL for this MCP. A dedicated
# ACL costs ~$8/mo in fixed AWS charges regardless of traffic; the shared ACL
# keeps this MCP's 300/5min limit as its own counter, aggregated on
# (IP, Host) so it stays independent of the other MCPs sharing that limit.
#
# The effective limit now lives in mcp-stats' `fleet_waf_members` under the key
# `living-atlas` — change it there, not here. The rate-limit value above is retained
# so that rolling back (use_shared_waf = false) restores the original limit.
# See mcp-stats/docs/waf-consolidation.md.
use_shared_waf = true
