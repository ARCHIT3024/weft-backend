# Weft — AWS Infrastructure Configuration

## Account Structure (Task 0.24)

| Environment | AWS Account | IAM Role | Purpose |
|-------------|-------------|----------|---------|
| Development | weft-dev | WeftDevAdmin | Local dev + CI testing |
| Staging | weft-staging | WeftStagingDeploy | Pre-prod validation |
| Production | weft-prod | WeftProdDeploy | Live service |

## ECR Repositories (Task 0.25)

```bash
aws ecr create-repository --repository-name weft-api --region ap-south-1
aws ecr create-repository --repository-name weft-tf-serving --region ap-south-1
```

## Staging Infrastructure (Tasks 0.27–0.29)

### RDS (PostgreSQL 15 + PostGIS)
```bash
aws rds create-db-instance \
  --db-instance-identifier weft-staging-db \
  --db-instance-class db.t3.small \
  --engine postgres \
  --engine-version 15 \
  --master-username weft_admin \
  --master-user-password <from-secrets-manager> \
  --allocated-storage 20 \
  --storage-type gp3 \
  --vpc-security-group-ids sg-xxx \
  --db-subnet-group-name weft-staging-subnets \
  --backup-retention-period 7 \
  --region ap-south-1
```

### ElastiCache (Redis 7)
```bash
aws elasticache create-cache-cluster \
  --cache-cluster-id weft-staging-redis \
  --engine redis \
  --engine-version 7.1 \
  --cache-node-type cache.t3.micro \
  --num-cache-nodes 1 \
  --region ap-south-1
```

### S3 Bucket (Issue Images)
```bash
aws s3api create-bucket \
  --bucket weft-issues-staging \
  --region ap-south-1 \
  --create-bucket-configuration LocationConstraint=ap-south-1

# Lifecycle policy: move to IA after 90 days, Glacier after 365 days
aws s3api put-bucket-lifecycle-configuration \
  --bucket weft-issues-staging \
  --lifecycle-configuration '{
    "Rules": [{
      "ID": "archive-old-images",
      "Status": "Enabled",
      "Transitions": [
        {"Days": 90, "StorageClass": "STANDARD_IA"},
        {"Days": 365, "StorageClass": "GLACIER"}
      ]
    }]
  }'
```

### Secrets Manager
```bash
aws secretsmanager create-secret --name weft/staging/DATABASE_URL --secret-string "postgresql+asyncpg://..."
aws secretsmanager create-secret --name weft/staging/JWT_PRIVATE_KEY --secret-string "$(cat private.pem)"
aws secretsmanager create-secret --name weft/staging/FCM_SERVICE_ACCOUNT --secret-string "$(cat fcm-service-account.json)"
```
