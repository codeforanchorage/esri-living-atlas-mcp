lambda_name = "sandiego-gis-mcp-prod"
stage_name  = "prod"
aws_region  = "us-west-2"
config_file = "config.yaml"
lambda_memory  = 512
lambda_timeout = 120
api_quota_limit = 3000
api_rate_limit  = 5
api_burst_limit = 10

# No custom domain yet: the API Gateway URL is used directly. To add one,
# set e.g. "sandiego-gis.codeforanchorage.org" here and follow the ACM
# validation + CNAME steps in the README.
custom_domain = ""

# Cap concurrent Lambda executions. Cost and blast-radius protection if
# WAF is bypassed via distributed sources. Conversational MCP traffic does
# not need horizontal scale; raise if legitimate users start getting throttled.
lambda_reserved_concurrency = 10

# WAF per-IP rate limit (rolling 5-minute window). The MCP tools are
# conversational, so 1 rps sustained per IP (~300/5min) is plenty for
# real users and tight enough to slow scrapers and denial-of-wallet probes.
waf_rate_limit_per_5min = 300
