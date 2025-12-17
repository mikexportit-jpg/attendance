# attendance_app/__init__.py

from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_migrate import Migrate

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your_secret_key_here'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///attendance.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
migrate = Migrate(app, db)

login_manager = LoginManager(app)
login_manager.login_view = 'auth.login'  # your login route name

# Import blueprints and routes after app and db are created
from attendance_app.auth import auth
from attendance_app import routes
from attendance_app.routes import api_bp

app.register_blueprint(auth, url_prefix='/auth')
app.register_blueprint(api_bp)
