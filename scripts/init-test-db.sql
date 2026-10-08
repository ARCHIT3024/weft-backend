-- Create the test database for pytest
-- This script runs on first container startup via docker-entrypoint-initdb.d
CREATE DATABASE weft_test OWNER weft;
\c weft_test
CREATE EXTENSION IF NOT EXISTS postgis;
