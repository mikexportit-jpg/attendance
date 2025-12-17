from attendance_app import app, db
from attendance_app.models import QRCodeToken
import secrets

with app.app_context():  # <- this provides the app context
    # Generate a token
    token = secrets.token_urlsafe(16)
    
    # Create QRCodeToken entry
    qr_entry = QRCodeToken(token=token)
    
    # Add to DB and commit
    db.session.add(qr_entry)
    db.session.commit()

    print("QR token created:", token)
