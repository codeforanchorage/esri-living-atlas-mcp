lambda_name = "living-atlas-mcp-staging"
stage_name  = "staging"
aws_region  = "us-west-2"
config_file = "config.yaml"
lambda_memory  = 512
lambda_timeout = 120

# Staging gets tighter quotas than prod: it exists for smoke tests, not
# traffic. No custom domain -- use the raw API Gateway invoke URL.
api_quota_limit = 1000
api_rate_limit  = 5
api_burst_limit = 10

lambda_reserved_concurrency = 5
waf_rate_limit_per_5min     = 300
