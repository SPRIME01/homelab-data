#!/usr/bin/env python3

import os
import sys
import json
import time
import logging
import requests
import tarfile
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from cryptography.fernet import Fernet
import yaml
import schedule
import argparse
from typing import Dict, List, Optional
import boto3
from botocore.exceptions import ClientError

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('/var/log/n8n/workflow-backup.log')
    ]
)
logger = logging.getLogger(__name__)

class N8nBackup:
    def __init__(self, config_path: str):
        """Initialize the backup system with configuration."""
        self.config = self._load_config(config_path)
        self.n8n_api_url = self.config['n8n']['api_url']
        self.n8n_api_key = self.config['n8n']['api_key']
        self.backup_dir = Path(self.config['backup']['local_path'])
        self.encryption_key = self._get_encryption_key()
        self.s3_client = self._init_s3_client() if self.config['storage'].get('type') == 's3' else None

    def _load_config(self, config_path: str) -> dict:
        """Load configuration from YAML file."""
        try:
            with open(config_path) as f:
                config = yaml.safe_load(f)
            self._validate_config(config)
            return config
        except Exception as e:
            logger.error(f"Failed to load configuration: {e}")
            sys.exit(1)

    def _validate_config(self, config: dict) -> None:
        """Validate the configuration structure."""
        required_keys = ['n8n', 'backup', 'storage', 'retention']
        for key in required_keys:
            if key not in config:
                raise ValueError(f"Missing required configuration section: {key}")

    def _get_encryption_key(self) -> bytes:
        """Get or generate encryption key."""
        key_path = self.backup_dir / '.encryption_key'
        if key_path.exists():
            return key_path.read_bytes()
        else:
            key = Fernet.generate_key()
            key_path.write_bytes(key)
            return key

    def _init_s3_client(self) -> Optional[boto3.client]:
        """Initialize S3 client if S3 storage is configured."""
        try:
            return boto3.client(
                's3',
                aws_access_key_id=self.config['storage']['s3']['access_key'],
                aws_secret_access_key=self.config['storage']['s3']['secret_key'],
                endpoint_url=self.config['storage']['s3'].get('endpoint_url')
            )
        except Exception as e:
            logger.error(f"Failed to initialize S3 client: {e}")
            return None

    def fetch_workflows(self) -> List[Dict]:
        """Fetch all workflows from n8n API."""
        try:
            response = requests.get(
                f"{self.n8n_api_url}/workflows",
                headers={"X-N8N-API-KEY": self.n8n_api_key}
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Failed to fetch workflows: {e}")
            return []

    def create_backup(self) -> Optional[Path]:
        """Create a backup of all workflows."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = self.backup_dir / f"n8n_backup_{timestamp}"

        try:
            # Create backup directory
            backup_path.mkdir(parents=True, exist_ok=True)

            # Fetch and save workflows
            workflows = self.fetch_workflows()
            if not workflows:
                raise ValueError("No workflows fetched")

            # Save individual workflows
            for workflow in workflows:
                workflow_file = backup_path / f"workflow_{workflow['id']}.json"
                with open(workflow_file, 'w') as f:
                    json.dump(workflow, f, indent=2)

            # Create metadata file
            metadata = {
                'timestamp': timestamp,
                'workflow_count': len(workflows),
                'n8n_version': self._get_n8n_version(),
                'backup_type': 'full'
            }
            with open(backup_path / 'metadata.json', 'w') as f:
                json.dump(metadata, f, indent=2)

            # Create compressed archive
            archive_path = self._compress_backup(backup_path, timestamp)

            # Encrypt the archive
            encrypted_path = self._encrypt_backup(archive_path)

            # Cleanup temporary files
            shutil.rmtree(backup_path)
            archive_path.unlink()

            return encrypted_path

        except Exception as e:
            logger.error(f"Backup creation failed: {e}")
            if backup_path.exists():
                shutil.rmtree(backup_path)
            return None

    def _get_n8n_version(self) -> str:
        """Get n8n version from the API."""
        try:
            response = requests.get(f"{self.n8n_api_url}/version")
            return response.json()['version']
        except:
            return 'unknown'

    def _compress_backup(self, backup_path: Path, timestamp: str) -> Path:
        """Compress the backup directory."""
        archive_path = self.backup_dir / f"n8n_backup_{timestamp}.tar.gz"
        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(backup_path, arcname=backup_path.name)
        return archive_path

    def _encrypt_backup(self, file_path: Path) -> Path:
        """Encrypt the backup file."""
        encrypted_path = file_path.with_suffix(file_path.suffix + '.enc')
        f = Fernet(self.encryption_key)

        with open(file_path, 'rb') as in_file, open(encrypted_path, 'wb') as out_file:
            out_file.write(f.encrypt(in_file.read()))

        return encrypted_path

    def upload_to_storage(self, backup_path: Path) -> bool:
        """Upload backup to configured storage."""
        storage_type = self.config['storage']['type']

        if storage_type == 's3':
            return self._upload_to_s3(backup_path)
        elif storage_type == 'local':
            return self._copy_to_local_storage(backup_path)
        else:
            logger.error(f"Unsupported storage type: {storage_type}")
            return False

    def _upload_to_s3(self, backup_path: Path) -> bool:
        """Upload backup to S3."""
        if not self.s3_client:
            return False

        try:
            bucket = self.config['storage']['s3']['bucket']
            key = f"n8n-backups/{backup_path.name}"

            self.s3_client.upload_file(
                str(backup_path),
                bucket,
                key,
                ExtraArgs={'ServerSideEncryption': 'AES256'}
            )
            return True
        except Exception as e:
            logger.error(f"S3 upload failed: {e}")
            return False

    def _copy_to_local_storage(self, backup_path: Path) -> bool:
        """Copy backup to local storage location."""
        try:
            dest_path = Path(self.config['storage']['local']['path'])
            dest_path.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup_path, dest_path / backup_path.name)
            return True
        except Exception as e:
            logger.error(f"Local storage copy failed: {e}")
            return False

    def cleanup_old_backups(self) -> None:
        """Remove old backups based on retention policy."""
        retention_days = self.config['retention']['days']
        max_backups = self.config['retention']['max_backups']

        # List all backup files
        backup_files = sorted(
            self.backup_dir.glob("n8n_backup_*.enc"),
            key=lambda x: x.stat().st_mtime
        )

        # Remove old backups based on age
        cutoff_date = datetime.now() - timedelta(days=retention_days)
        for backup_file in backup_files:
            if datetime.fromtimestamp(backup_file.stat().st_mtime) < cutoff_date:
                backup_file.unlink()
                logger.info(f"Removed old backup: {backup_file.name}")

        # Remove excess backups based on max_backups
        backup_files = sorted(
            self.backup_dir.glob("n8n_backup_*.enc"),
            key=lambda x: x.stat().st_mtime
        )
        if len(backup_files) > max_backups:
            for backup_file in backup_files[:-max_backups]:
                backup_file.unlink()
                logger.info(f"Removed excess backup: {backup_file.name}")

    def run_backup(self) -> None:
        """Run the complete backup process."""
        logger.info("Starting n8n workflow backup")

        # Create backup
        backup_path = self.create_backup()
        if not backup_path:
            logger.error("Backup creation failed")
            return

        # Upload to storage
        if self.upload_to_storage(backup_path):
            logger.info(f"Backup uploaded successfully: {backup_path.name}")
        else:
            logger.error(f"Failed to upload backup: {backup_path.name}")

        # Cleanup old backups
        self.cleanup_old_backups()

def main():
    parser = argparse.ArgumentParser(description='n8n Workflow Backup Tool')
    parser.add_argument('--config', default='/etc/n8n/backup-config.yml',
                      help='Path to configuration file')
    parser.add_argument('--run-once', action='store_true',
                      help='Run backup once and exit')
    args = parser.parse_args()

    backup_system = N8nBackup(args.config)

    if args.run_once:
        backup_system.run_backup()
    else:
        # Schedule regular backups
        schedule_config = backup_system.config['backup']['schedule']
        schedule.every().day.at(schedule_config['time']).do(backup_system.run_backup)

        logger.info("Starting scheduled backup service")
        while True:
            schedule.run_pending()
            time.sleep(60)

if __name__ == "__main__":
    main()
