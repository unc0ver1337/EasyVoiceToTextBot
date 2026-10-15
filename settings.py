"""Per-user preferences; transcripts and audio are never stored here."""
import sqlite3
from pathlib import Path
class Settings:
    def __init__(self, path):
        self.db=sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS preferences (user_id INTEGER PRIMARY KEY, language TEXT NOT NULL DEFAULT 'auto', mode TEXT NOT NULL DEFAULT 'quality')")
        self.db.commit()
    def get(self, uid):
        row=self.db.execute('SELECT language,mode FROM preferences WHERE user_id=?',(uid,)).fetchone()
        return row or ('auto','quality')
    def set(self, uid, field, value):
        if field not in {'language','mode'}: raise ValueError('Unknown setting')
        if field=='language' and value not in {'auto','kk','ru','en','uk','uz','tr','de','fr','zh'}: raise ValueError('Unsupported language')
        if field=='mode' and value not in {'quality','fast'}: raise ValueError('Unsupported mode')
        self.db.execute('INSERT OR IGNORE INTO preferences(user_id) VALUES(?)',(uid,))
        self.db.execute(f'UPDATE preferences SET {field}=? WHERE user_id=?',(value,uid))
        self.db.commit()
