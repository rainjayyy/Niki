-- Niki — PostgreSQL initialization script
-- This runs on first container startup only.

-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";   -- For fuzzy text search

-- Set default configuration
ALTER DATABASE niki SET timezone TO 'Asia/Shanghai';

-- Placeholder: tables will be created by Alembic migrations
-- This file exists mainly to establish the database and extensions.
