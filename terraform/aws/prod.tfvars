lambda_name = "living-atlas-mcp-prod"
stage_name  = "prod"
aws_region  = "us-west-2"
config_file = "config.yaml"
lambda_memory  = 512
lambda_timeout = 120
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
