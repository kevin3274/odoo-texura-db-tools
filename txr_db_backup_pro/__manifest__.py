{
    "name": "Texura DB Backup Pro",
    "version": "19.0.1.0.0",
    "category": "Technical",
    "summary": "Cloud backup with AES-256 encryption, L3 restore verification & one-click restore",
    "description": """
Texura DB Backup Pro — production-grade backup for Odoo
========================================================

Builds on Texura DB Backup Lite with the four capabilities competitors lack:

* **Cloud Storage (rclone)** — S3 / R2 / MinIO, Google Cloud Storage,
  Azure Blob, Backblaze B2, Nextcloud / WebDAV, custom rclone configs
* **AES-256-GCM Encryption** — client-side, key kept by you, exportable
* **L3 Restore Verification** — actually restores into a temp DB,
  runs critical-table queries, drops the temp DB; the only Odoo backup
  module that proves your backups are restorable
* **One-Click Restore Wizard** — pick a successful backup, enter target
  DB name, hit Restore; download / decrypt / restore happens async via
  ir.cron with full progress + history

Plus production-friendly features:

* L3 maintenance window + ionice/nice resource priority
* Pre-flight disk space checks
* Strategy auto-recommendation (standard / split / streaming) for
  large databases (5GB+ filestore handled by split mode without
  filling temp disk)
""",
    "author": "Texura",
    "website": "https://apps.odoo.com/apps/modules/browse?author=Texura",
    "license": "OPL-1",
    "price": 0.0,
    "currency": "EUR",
    "depends": ["txr_db_backup_lite"],
    "data": [
        "security/ir.model.access.csv",
        "data/ir_cron_data.xml",
        "views/db_backup_cloud_views.xml",
        "views/db_backup_views.xml",
        "views/db_backup_log_views.xml",
        "views/db_restore_job_views.xml",
    ],
    "support": "kevin@loyal-info.com",
    "images": ["static/description/icon.png"],
    "installable": True,
    "auto_install": False,
}
