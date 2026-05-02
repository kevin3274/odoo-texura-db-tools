{
    "name": "Texura DB Backup Lite",
    "version": "19.0.1.0.0",
    "category": "Technical",
    "summary": "Zero-dependency database backup with verification",
    "author": "Texura",
    "website": "https://apps.odoo.com/apps/modules/browse?author=Texura",
    "license": "LGPL-3",
    "depends": ["base", "mail"],
    "data": [
        "security/ir.model.access.csv",
        "views/db_backup_log_views.xml",
        "views/db_backup_views.xml",
    ],
    "support": "kevin@loyal-info.com",
    "images": ["images/main_screenshot.png", "images/logs_screenshot.png", "images/sftp_screenshot.png"],
    "installable": True,
    "auto_install": False,
}
