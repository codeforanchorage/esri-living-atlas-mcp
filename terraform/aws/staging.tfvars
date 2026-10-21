lambda_name = "living-atlas-mcp-staging"
stage_name  = "staging"
aws_region  = "us-west-2"
config_file = "config.yaml"
lambda_memory  = 512
# Overridden by aws.lambda_timeout in config.yaml (main.tf reads config
# first); kept equal to it so the two cannot drift. 28s sits under API
# Gateway's hard 29s integration timeout -- see config.yaml.
lambda_timeout = 28

# Staging gets tighter quotas than prod: it exists for smoke tests, not
# traffic. No custom domain -- use the raw API Gateway invoke URL.
api_quota_limit = 1000
api_rate_limit  = 5
api_burst_limit = 10

lambda_reserved_concurrency = 5

# WAF per-IP rate limit (rolling 5-minute window). Not read while
# use_shared_waf is true -- retained so that rolling back (use_shared_waf =
# false) recreates this repo's own ACL at the original limit. Prod uses the
# same pattern.
waf_rate_limit_per_5min = 300

# Delegate to the fleet-wide ACL (mcp-fleet-waf) instead of standing up a
# dedicated one. WAF bills fixed monthly regardless of traffic -- $5 per web
# ACL + $1 per rule -- so this repo's 3-rule staging ACL is ~$8/mo to
# duplicate protection the fleet ACL already provides.
#
# Staging has no custom domain mapping, so its host is the raw
# kcj6gkp5gj.execute-api.us-west-2.amazonaws.com. That is not a fleet member,
# so it lands on the fleet ACL's rate-limit-unmatched-host catch-all: 300 per
# 5 min aggregated by IP, plus the same KnownBadInputs and CommonRuleSet
# managed groups the dedicated ACL had. Identical limit, identical rules.
#
# No mcp-stats change needed: fleet membership only matters for a non-default
# limit, and staging wants the default. Prod stays an explicit member at
# 300/5min under living-atlas.codeforanchorage.org.
# See mcp-stats/docs/waf-consolidation.md.
use_shared_waf = true
