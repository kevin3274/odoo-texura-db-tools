{
    "name": "Texura DB Backup Lite",
    "version": "18.0.1.0.0",
    "category": "Technical",
    "summary": "Zero-dependency database backup with verification",
    "author": "Texura",
    "website": "",
    "license": "LGPL-3",
    "depends": ["base", "mail"],
    "data": [
        "security/ir.model.access.csv",
        "views/db_backup_log_views.xml",
        "views/db_backup_views.xml",
    ],
    "support": "kevin@loyal-info.com",
    "images": ["static/description/screenshot_config.png", "static/description/screenshot_sftp.png", "static/description/screenshot_logs.png"],
    "installable": True,
    "auto_install": False,
}
