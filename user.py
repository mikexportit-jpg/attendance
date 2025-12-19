# user.py
from attendance_app import app, db
from attendance_app.models import User
from werkzeug.security import generate_password_hash

# Use application context
with app.app_context():
    # Hash your desired password
    hashed_password = generate_password_hash("admin123")  # change to your secure password

    # Create the admin user
    admin = User(
        name="Admin User",
        username="admin",
        password=hashed_password,
        role="admin",
        salary_per_month=0.0,
        device_id=None,
        serial_number=None,
        is_admin=True
    )

    # Add to database and commit
    db.session.add(admin)
    db.session.commit()

    print("Admin user created successfully!")
