terraform {
  backend "s3" {
    bucket         = "worcester-gis-opencontext-tfstate"
    key            = "terraform.tfstate"
    region         = "us-west-2"
    dynamodb_table = "terraform-state-lock"
    encrypt        = true
  }
}
