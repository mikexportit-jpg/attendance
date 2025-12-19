from datetime import datetime, time, date
from attendance_app import db, login_manager
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    username = db.Column(db.String(100), unique=True, nullable=False)
    password = db.Column(db.String(128))
    role = db.Column(db.String(20), default='employee')
    salary_per_month = db.Column(db.Float, default=0.0)
    device_id = db.Column(db.String(100), unique=True, nullable=True)
    serial_number = db.Column(db.String(255), unique=True, nullable=True)
    is_admin = db.Column(db.Boolean, default=False)

    def set_password(self, password):
        self.password = password

    def check_password(self, password):
        return self.password == password

class Attendance(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    date = db.Column(db.Date, nullable=False)
    clock_in = db.Column(db.Time, nullable=False)
    clock_out = db.Column(db.Time, nullable=True)
    overtime = db.Column(db.Float)
    late_minutes = db.Column(db.Integer)
    
    user = db.relationship('User', backref='attendances')

    @property
    def total_hours(self):
        if self.clock_in and self.clock_out:
            start = datetime.combine(self.date, self.clock_in)
            end = datetime.combine(self.date, self.clock_out)
            duration = end - start
            return round(duration.total_seconds() / 3600, 2)
        return 0.0  
    @property
    def regular_hours(self):
        if self.total_hours and self.overtime:
            return round(self.total_hours - self.overtime, 2)
        return self.total_hours or 0.0

class AdvanceSalary(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    amount = db.Column(db.Float, nullable=False)
    date = db.Column(db.Date, default=date.today)
    user = db.relationship('User', backref='advances')

class BreakSession(db.Model):
    __tablename__ = 'break_session'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    attendance_id = db.Column(db.Integer, db.ForeignKey('attendance.id'))
    date = db.Column(db.Date, nullable=False)
    start_time = db.Column(db.Time, nullable=False)
    end_time = db.Column(db.Time, nullable=True)

    user = db.relationship('User', backref='break_sessions')
    attendance = db.relationship('Attendance', backref='break_sessions')

    @property
    def duration_minutes(self):
        if self.break_start and self.break_end:
            start = datetime.combine(date.today(), self.break_start)
            end = datetime.combine(date.today(), self.break_end)
            return round((end - start).total_seconds() / 60)
        return 0

class LeaveRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date, nullable=False)
    reason = db.Column(db.String(200))
    status = db.Column(db.String(20), default='Pending')  # Pending, Approved, Rejected
    user = db.relationship('User', backref='leave_requests')    

class Overtime(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    user = db.relationship('User', backref='overtimes')
    date = db.Column(db.Date, nullable=False)
    hours = db.Column(db.Float, nullable=False)
    reason = db.Column(db.String(255))

class Deduction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    amount = db.Column(db.Float, nullable=False)
    reason = db.Column(db.String(200))
    date = db.Column(db.Date, default=date.today)
    user = db.relationship('User', backref='deductions')

class QRCodeToken(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(64), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    used = db.Column(db.Boolean, default=False)    