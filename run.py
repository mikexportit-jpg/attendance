from attendance_app import app, db
import os

def init_db():
    if not os.path.exists('attendance.db'):
        with app.app_context():
            db.create_all()
            print("Database created!")
    else:
        print("Database already exists.")

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=5000)
    with app.app_context():
        db.create_all()
    app.run(debug=True)
