from flask import Blueprint, render_template, redirect, url_for, request, flash, send_file, jsonify, make_response, abort
from flask_login import login_required, current_user
from datetime import datetime, date, time, timedelta
from sqlalchemy import func, cast, Date, and_
from calendar import monthrange
import calendar, io, qrcode, os, secrets, pandas as pd
from werkzeug.utils import secure_filename
import requests
from flask_weasyprint import HTML
from attendance_app import app, db
from attendance_app.models import (
    User, Attendance, AdvanceSalary, LeaveRequest, Overtime, BreakSession, QRCodeToken, Deduction
)
from attendance_app.forms import DeductionForm
from functools import wraps

api_bp = Blueprint('api', __name__)

WORK_SCHEDULE = {
    0: {'start': time(10, 0), 'end': time(19, 0)},  # Monday
    1: {'start': time(10, 0), 'end': time(19, 0)},  # Tuesday
    2: {'start': time(10, 0), 'end': time(19, 0)},  # Wednesday
    3: {'start': time(10, 0), 'end': time(19, 0)},  # Thursday
    4: {'start': time(10, 0), 'end': time(19, 0)},  # Friday
    5: {'start': time(10, 0), 'end': time(15, 0)},  # Saturday
    6: None,  # Sunday
}

LATE_THRESHOLD = time(10, 10)

@app.route('/')
@login_required
def dashboard():
    today = date.today()

    # Manager/Admin dashboard
    if current_user.role == 'manager':
        total_employees = User.query.filter_by(role='employee').count()
        total_attendance = Attendance.query.count()
        today_clock_ins = Attendance.query.filter(
            cast(Attendance.date, Date) == today,
            Attendance.clock_in != None
        ).count()
        today_overtime = sum(
            (a.overtime or 0) for a in Attendance.query.filter(cast(Attendance.date, Date) == today).all()
        )

        recent_sessions = Attendance.query.order_by(Attendance.date.desc()).limit(10).all()

        return render_template(
            'admin_dashboard.html',
            total_employees=total_employees,
            total_attendance=total_attendance,
            today_clock_ins=today_clock_ins,
            today_overtime=today_overtime,
            recent_sessions=recent_sessions
        )

    # Employee dashboard
    sessions = Attendance.query.filter(
        Attendance.user_id == current_user.id,
        Attendance.date == today
    ).order_by(Attendance.clock_in).all()

    total_regular = timedelta()
    overtime = 0
    late_minutes = 0
    active_clock_in = None
    active_break = BreakSession.query.filter_by(
    user_id=current_user.id,
    date=today,
    end_time=None
).first()
    active_break_start = active_break.start_time if active_break else None

    for s in sessions:
        if s.clock_in and s.clock_out:
            duration = datetime.combine(today, s.clock_out) - datetime.combine(today, s.clock_in)
            total_regular += duration - timedelta(hours=s.overtime or 0)
            overtime += s.overtime or 0
            late_minutes += s.late_minutes or 0
        elif s.clock_in and not s.clock_out:
            active_clock_in = s.clock_in
            now = datetime.now()
            clock_in_datetime = datetime.combine(today, s.clock_in)
            duration = now - clock_in_datetime
            total_regular += duration

            # Check for active break
            last_break = BreakSession.query.filter_by(attendance_id=s.id).order_by(BreakSession.id.desc()).first()
            if last_break and last_break.break_end is None:
                active_break_start = last_break.break_start

    return render_template(
        'dashboard.html',
        today=today,
        sessions=sessions,
        total_regular=total_regular,
        overtime=overtime,
        late_minutes=late_minutes,
        active_clock_in=active_clock_in,
        active_break_start=active_break_start,
        active_break=active_break,
        datetime=datetime
    )

@app.route('/clock_in')
@login_required
def clock_in():
    today = date.today()
    weekday = today.weekday()
    now = datetime.now()
    now_time = now.time()

    # Check if there's an open session already (clock_out=None)
    open_session = Attendance.query.filter_by(user_id=current_user.id, date=today, clock_out=None).first()
    if open_session:
        flash("You already clocked in and haven't clocked out yet!", "warning")
        return redirect(url_for('dashboard'))

    attendance = Attendance(user_id=current_user.id, date=today, clock_in=now_time, clock_out=None)
    
    # Overtime and late logic per session:
    schedule = WORK_SCHEDULE.get(weekday)
    if schedule is None:
        # Sunday whole day overtime, example 9 hours
        attendance.overtime = 9
    else:
        attendance.overtime = 0
        attendance.late_minutes = 0

        if now_time < schedule['start']:
            early_seconds = (datetime.combine(today, schedule['start']) - datetime.combine(today, now_time)).seconds
            attendance.overtime += round(early_seconds / 3600, 2)

        if now_time > LATE_THRESHOLD:
            late_seconds = (datetime.combine(today, now_time) - datetime.combine(today, LATE_THRESHOLD)).seconds
            attendance.late_minutes = late_seconds // 60

    db.session.add(attendance)
    db.session.commit()

    flash("Clocked in successfully!", "success")
    return redirect(url_for('dashboard'))

@app.route('/clock_out')
@login_required
def clock_out():
    today = date.today()
    weekday = today.weekday()
    now = datetime.now()
    now_time = now.time()

    # Find open session for today
    open_session = Attendance.query.filter_by(user_id=current_user.id, date=today, clock_out=None).first()
    if not open_session:
        flash("No active clock-in session found!", "warning")
        return redirect(url_for('dashboard'))

    open_session.clock_out = now_time

    schedule = WORK_SCHEDULE.get(weekday)
    if schedule is None:
        open_session.overtime = 9
    else:
        if now_time > schedule['end']:
            late_seconds = (datetime.combine(today, now_time) - datetime.combine(today, schedule['end'])).seconds
            open_session.overtime = (open_session.overtime or 0) + round(late_seconds / 3600, 2)

    # ⏱️ Break time check
    total_break_minutes = 0
    for b in open_session.breaks:
        if b.break_start and b.break_end:
            start = datetime.combine(today, b.break_start)
            end = datetime.combine(today, b.break_end)
            total_break_minutes += round((end - start).total_seconds() / 60)

    # 🛑 Apply deduction if break > 60 mins
    allowed_minutes = 60
    if total_break_minutes > allowed_minutes:
        extra_minutes = total_break_minutes - allowed_minutes
        deduction_amount = extra_minutes * 1.0  # $1 per extra minute

        deduction = Deduction(
            user_id=current_user.id,
            date=today,
            amount=deduction_amount,
            reason=f"Excessive break time: {extra_minutes} mins"
        )
        db.session.add(deduction)
        flash(f"${deduction_amount:.2f} deducted for extra break time.", "danger")

    db.session.commit()
    flash("Clocked out successfully!", "success")
    return redirect(url_for('dashboard'))

@app.route('/users')
@login_required
def users():
    # Handle NFC UID update via POST (inline form in users table)
    if request.method == 'POST':
        user_id = request.form.get('user_id')
        nfc_uid = request.form.get('nfc_uid', '').strip()
        user = User.query.get(user_id)
        if user:
            user.nfc_uid = nfc_uid if nfc_uid else None
            db.session.commit()
            flash('NFC UID updated successfully.', 'success')
        else:
            flash('User not found.', 'danger')
        return redirect(url_for('users'))

    # GET - show list with search
    q = request.args.get('q', '').strip()
    users_query = User.query
    if q:
        users_query = users_query.filter(User.name.ilike(f'%{q}%'))
    users = users_query.order_by(User.name).all()
    return render_template('users.html', users=users)

@app.route('/users/add', methods=['GET', 'POST'])
@login_required
def add_user():
    if current_user.role != 'manager':
        flash("Access denied.", "danger")
        return redirect(url_for('users'))

    if request.method == 'POST':
        name = request.form.get('name')
        username = request.form.get('username')
        role = request.form.get('role')
        salary = request.form.get('salary')
        password = request.form.get('password')
        serial_number = request.form.get('serial_number')  # get from form by name attribute

        # Validate salary conversion
        try:
            salary = float(salary)
        except (TypeError, ValueError):
            flash("Invalid salary amount.", "danger")
            return redirect(url_for('add_user'))

        # Check for existing username
        if User.query.filter_by(username=username).first():
            flash("Username already exists", "danger")
            return redirect(url_for('add_user'))

        new_user = User(
            name=name,
            username=username,
            role=role,
            salary_per_month=salary,
            serial_number=serial_number  # set serial number here
        )
        new_user.set_password(password)

        db.session.add(new_user)
        db.session.commit()
        flash("User added successfully!", "success")
        return redirect(url_for('users'))

    return render_template('add_user.html')

@app.route('/advances')
@login_required
def advances():
    start_date_str = request.args.get('start_date', date.today().isoformat())
    end_date_str = request.args.get('end_date', date.today().isoformat())
    user_id = request.args.get('user_id')

    start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
    end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()

    query = AdvanceSalary.query.filter(
        AdvanceSalary.date >= start_date,
        AdvanceSalary.date <= end_date
    )

    users = []
    if current_user.role in ('manager',) or getattr(current_user, 'is_admin', False):
        users = User.query.order_by(User.name).all()
        if user_id:
            try:
                uid = int(user_id)
                query = query.filter(AdvanceSalary.user_id == uid)
            except ValueError:
                pass  # invalid user_id — skip filter
    else:
        query = query.filter(AdvanceSalary.user_id == current_user.id)

    advance_salaries = query.order_by(AdvanceSalary.date.desc()).all()

    # ✅ Calculate total advance amount
    total_advance_amount = query.with_entities(func.sum(AdvanceSalary.amount)).scalar() or 0.0

    return render_template('advances.html',
                           advance_salaries=advance_salaries,
                           users=users,
                           start_date=start_date_str,
                           end_date=end_date_str,
                           selected_user_id=user_id or '',
                           total_advance_amount=total_advance_amount)

@app.route('/advances/add', methods=['GET', 'POST'])
@login_required
def add_advance():
    if current_user.role != 'manager':
        flash("Access denied.", "danger")
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        user_id = int(request.form['user_id'])
        amount = float(request.form['amount'])

        new_advance = AdvanceSalary(user_id=user_id, amount=amount)
        db.session.add(new_advance)
        db.session.commit()
        flash("Advance salary added.", "success")
        return redirect(url_for('advances'))
    users = User.query.all()
    return render_template('add_advance.html', users=users)

@app.route('/users/edit/<int:user_id>', methods=['GET', 'POST'])
@login_required
def edit_user(user_id):
    user = User.query.get_or_404(user_id)

    if request.method == 'POST':
        user.name = request.form.get('name')
        user.username = request.form.get('username')
        user.role = request.form.get('role')
        salary = request.form.get('salary')
        serial_number = request.form.get('serial_number')  # get serial number from form

        try:
            user.salary_per_month = float(salary)
        except (TypeError, ValueError):
            flash("Invalid salary amount.", "danger")
            return redirect(url_for('edit_user', user_id=user_id))

        user.serial_number = serial_number

        password = request.form.get('password')
        if password:
            user.set_password(password)

        try:
            db.session.commit()
            flash('User updated successfully.', 'success')
        except Exception as e:
            db.session.rollback()
            flash(f'Error updating user: {str(e)}', 'danger')

        return redirect(url_for('users'))

    # GET method: render the edit form with current user data
    return render_template('edit_user.html', user=user)

@app.route('/users/delete/<int:user_id>', methods=['POST'])
@login_required
def delete_user(user_id):
    if current_user.role != 'manager':
        flash("Access denied.", "danger")
        return redirect(url_for('dashboard'))

    user = User.query.get_or_404(user_id)

    # Nullify user_id in attendance records before deleting user
    Attendance.query.filter_by(user_id=user.id).update({Attendance.user_id: None})
    
    db.session.delete(user)
    db.session.commit()

    flash("User deleted, but attendance records retained.", "success")
    return redirect(url_for('users'))

@app.route('/leave-requests')
@login_required
def leave_requests():
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    user_id = request.args.get('user_id')

    query = LeaveRequest.query

    if current_user.role != 'manager' and not current_user.is_admin:
        query = query.filter_by(user_id=current_user.id)
    else:
        if user_id:
            query = query.filter_by(user_id=user_id)

    if start_date and end_date:
        query = query.filter(and_(
            LeaveRequest.start_date >= start_date,
            LeaveRequest.end_date <= end_date
        ))

    leave_requests = query.order_by(LeaveRequest.start_date.desc()).all()

    users = User.query.all() if current_user.role == 'manager' or current_user.is_admin else []

    return render_template('leave_requests.html', leave_requests=leave_requests,
                           start_date=start_date, end_date=end_date, user_id=user_id, users=users)

@app.route('/leave-requests/export/<string:file_type>')
@login_required
def leave_requests_export(file_type):
    query = LeaveRequest.query

    if current_user.role != 'manager' and not current_user.is_admin:
        query = query.filter_by(user_id=current_user.id)
    else:
        user_id = request.args.get('user_id')
        if user_id:
            query = query.filter_by(user_id=user_id)

    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    if start_date and end_date:
        query = query.filter(and_(
            LeaveRequest.start_date >= start_date,
            LeaveRequest.end_date <= end_date
        ))

    leave_requests = query.order_by(LeaveRequest.start_date.desc()).all()

    data = []
    for lr in leave_requests:
        data.append({
            'Employee': lr.user.name if lr.user else "N/A",
            'Start Date': lr.start_date,
            'End Date': lr.end_date,
            'Reason': lr.reason,
            'Status': lr.status,
        })

    if file_type == 'excel':
        import xlsxwriter
        output = io.BytesIO()
        df = pd.DataFrame(data)
        with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
            df.to_excel(writer, index=False, sheet_name='Leave Requests')
        output.seek(0)
        return send_file(output, download_name="leave_requests.xlsx", as_attachment=True)

    elif file_type == 'pdf':
        from flask_weasyprint import HTML, render_pdf
        html = render_template('leave_requests_pdf.html', leave_requests=leave_requests)
        return render_pdf(HTML(string=html))

    return "Unsupported file type", 400

@app.route('/overtime-reports')
@login_required
def overtime_reports():
    start_date_str = request.args.get('start_date')
    end_date_str = request.args.get('end_date')
    user_id = request.args.get('user_id')

    # Default to today if no dates provided
    today = datetime.today().date()
    start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date() if start_date_str else today
    end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date() if end_date_str else today

    query = Attendance.query.filter(
        Attendance.date >= start_date,
        Attendance.date <= end_date,
        Attendance.overtime > 0
    )
    if user_id:
        query = query.filter(Attendance.user_id == int(user_id))

    records = query.order_by(Attendance.date.desc()).all()
    users = User.query.order_by(User.name).all()

    return render_template('overtime_reports.html', records=records, users=users, current_date=today)

@app.route('/report')
@login_required
def report():
    start_date_str = request.args.get('start_date')
    end_date_str = request.args.get('end_date')
    user_id = request.args.get('user_id')
    today_str = date.today().isoformat()

    if not start_date_str:
        start_date_str = today_str
    if not end_date_str:
        end_date_str = today_str

    start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
    end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()

    # Base attendance query for daily data
    daily_query = Attendance.query.filter(
        Attendance.date >= start_date,
        Attendance.date <= end_date,
    )

    if current_user.role in ('manager',) or getattr(current_user, 'is_admin', False):
        if user_id:
            try:
                uid = int(user_id)
                daily_query = daily_query.filter(Attendance.user_id == uid)
            except ValueError:
                pass
        users = User.query.order_by(User.name).all()
    else:
        daily_query = daily_query.filter(Attendance.user_id == current_user.id)
        users = []

    # Fetch raw attendance rows for daily report
    daily_rows = daily_query.order_by(Attendance.date.asc()).all()

    # Build daily_data with sums calculated in Python
    daily_data = []
    for att in daily_rows:
        daily_data.append({
            'id': att.id,
            'date': att.date.strftime('%Y-%m-%d'),
            'user_id': att.user_id,
            'user': att.user,
            'clock_in': att.clock_in.strftime('%H:%M:%S') if att.clock_in else None,
            'clock_out': att.clock_out.strftime('%H:%M:%S') if att.clock_out else None,
            'regular_hours': round(att.regular_hours or 0, 2),  # property
            'overtime': round(att.overtime or 0, 2),
            'total_hours': round(att.total_hours or 0, 2),      # property
            'late_minutes': att.late_minutes or 0,
        })

    # Weekly aggregation - get overtime per week and user from DB (real columns only)
    weekly_raw = db.session.query(
        func.strftime('%Y-%W', Attendance.date).label('week'),
        Attendance.user_id,
        func.sum(Attendance.overtime).label('overtime')
    ).filter(
        Attendance.date >= start_date,
        Attendance.date <= end_date,
    )

    if current_user.role in ('manager',) or getattr(current_user, 'is_admin', False):
        if user_id:
            try:
                uid = int(user_id)
                weekly_raw = weekly_raw.filter(Attendance.user_id == uid)
            except ValueError:
                pass
    else:
        weekly_raw = weekly_raw.filter(Attendance.user_id == current_user.id)

    weekly_raw = weekly_raw.group_by('week', Attendance.user_id).all()

    # Map users for weekly report
    weekly_user_ids = {row.user_id for row in weekly_raw}
    weekly_users = User.query.filter(User.id.in_(weekly_user_ids)).all()
    user_map = {u.id: u for u in weekly_users}

    # Build weekly_data - sum regular_hours and total_hours in Python
    weekly_data = []
    for row in weekly_raw:
        week_start = datetime.strptime(row.week + '-1', "%Y-%W-%w").date()
        week_end = week_start + timedelta(days=6)

        attendances = Attendance.query.filter(
            Attendance.user_id == row.user_id,
            Attendance.date >= week_start,
            Attendance.date <= week_end,
        ).all()

        regular_hours_sum = sum(att.regular_hours or 0 for att in attendances)
        total_hours_sum = sum(att.total_hours or 0 for att in attendances)

        weekly_data.append({
            'week': row.week,
            'user': user_map.get(row.user_id),
            'regular_hours': round(regular_hours_sum, 2),
            'total_hours': round(total_hours_sum, 2),
            'overtime': round(row.overtime or 0, 2),
        })

    # Monthly aggregation - same approach, only aggregate real DB columns in query
    monthly_raw = db.session.query(
        func.strftime('%Y-%m', Attendance.date).label('month'),
        Attendance.user_id,
        func.count(func.distinct(Attendance.date)).label('work_days'),
        func.sum(Attendance.overtime).label('overtime')
    ).filter(
        Attendance.date >= start_date,
        Attendance.date <= end_date,
        Attendance.user_id != None
    )

    if current_user.role in ('manager',) or getattr(current_user, 'is_admin', False):
        if user_id:
            try:
                uid = int(user_id)
                monthly_raw = monthly_raw.filter(Attendance.user_id == uid)
            except ValueError:
                pass
    else:
        monthly_raw = monthly_raw.filter(Attendance.user_id == current_user.id)

    monthly_raw = monthly_raw.group_by('month', Attendance.user_id).all()

    # Map users for monthly report
    user_ids = {row.user_id for row in monthly_raw}
    user_map = {user.id: user for user in User.query.filter(User.id.in_(user_ids)).all()}

    monthly_data = []
    for row in monthly_raw:
        user_obj = user_map.get(row.user_id)
        salary = user_obj.salary_per_month if user_obj else 0

        year, month = map(int, str(row.month).split('-'))
        month_start = date(year, month, 1)
        days_in_month = monthrange(year, month)[1]
        month_end = date(year, month, days_in_month)

        attendances = Attendance.query.filter(
            Attendance.user_id == row.user_id,
            Attendance.date >= month_start,
            Attendance.date <= month_end,
        ).all()

        regular_hours_sum = sum(att.regular_hours or 0 for att in attendances)
        overtime_hours = sum(att.overtime or 0 for att in attendances)
        total_hours_sum = sum(att.total_hours or 0 for att in attendances)

        total_regular_month_hours = 0
        for day_offset in range(days_in_month):
            day = month_start + timedelta(days=day_offset)
            if day.weekday() < 5:
                total_regular_month_hours += 9
            elif day.weekday() == 5:
                total_regular_month_hours += 5

        hourly_rate = salary / total_regular_month_hours if total_regular_month_hours > 0 else 0
        regular_pay = regular_hours_sum * hourly_rate
        overtime_rate = hourly_rate * 1.5
        overtime_payment = overtime_hours * overtime_rate

        deductions = Deduction.query.filter(
            Deduction.user_id == row.user_id,
            Deduction.date >= month_start,
            Deduction.date <= month_end
        ).with_entities(func.coalesce(func.sum(Deduction.amount), 0)).scalar() or 0

        advances = AdvanceSalary.query.filter(
            AdvanceSalary.user_id == row.user_id,
            AdvanceSalary.date >= month_start,
            AdvanceSalary.date <= month_end
        ).with_entities(func.coalesce(func.sum(AdvanceSalary.amount), 0)).scalar() or 0

        total_salary = regular_pay + overtime_payment - deductions - advances
        working_salary = regular_pay + overtime_payment

        monthly_data.append({
            'month': row.month,
            'user': user_obj,
            'work_days': row.work_days,
            'total_hours': round(total_hours_sum, 2),
            'regular_hours': round(regular_hours_sum, 2),
            'overtime': round(overtime_hours, 2),
            'hourly_rate': round(hourly_rate, 2),
            'regular_pay': round(regular_pay, 2),
            'overtime_rate': round(overtime_rate, 2),
            'overtime_payment': round(overtime_payment, 2),
            'deductions': round(deductions, 2),
            'advances': round(advances, 2),
            'total_salary': round(total_salary, 2),
            'working_salary': round(working_salary, 2),
        })

    currency = "$"

    return render_template(
        'report.html',
        current_date=today_str,
        daily_data=daily_data,
        weekly_data=weekly_data,
        monthly_data=monthly_data,
        users=users,
        currency=currency,
        active_tab='daily'  # default tab selected
    )

@app.route('/attendance/edit/<int:attendance_id>', methods=['GET', 'POST'])
@login_required
def edit_attendance(attendance_id):
    if current_user.role != 'admin' and current_user.role != 'manager':
        flash("Access denied.", "danger")
        return redirect(url_for('dashboard'))

    attendance = Attendance.query.get_or_404(attendance_id)

    if request.method == 'POST':
        try:
            attendance.user_id = int(request.form['user_id'])

            # Date
            date_str = request.form['date']
            attendance.date = datetime.strptime(date_str, '%Y-%m-%d').date()

            # Time parsing
            clock_in_str = request.form.get('clock_in')
            clock_out_str = request.form.get('clock_out')

            clock_in = datetime.strptime(clock_in_str, '%H:%M').time() if clock_in_str else None
            clock_out = datetime.strptime(clock_out_str, '%H:%M').time() if clock_out_str else None

            attendance.clock_in = clock_in
            attendance.clock_out = clock_out

            # Recalculate based on rules
            attendance.overtime, attendance.late_minutes = calculate_attendance_metrics(attendance.date, clock_in, clock_out)

            db.session.commit()
            flash("Attendance updated successfully!", "success")
            return redirect(url_for('report'))

        except Exception as e:
            db.session.rollback()
            flash(f"Error updating attendance: {e}", "danger")

    users = User.query.all()
    return render_template('edit_attendance.html', attendance=attendance, users=users)
@app.route('/attendance/add', methods=['GET', 'POST'])
@login_required
def add_attendance():
    if current_user.role != 'admin' and current_user.role != 'manager':
        flash("Access denied.", "danger")
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        try:
            user_id = int(request.form['user_id'])
            date = datetime.strptime(request.form['date'], '%Y-%m-%d').date()
            clock_in_str = request.form.get('clock_in')
            clock_out_str = request.form.get('clock_out')

            clock_in = datetime.strptime(clock_in_str, '%H:%M').time() if clock_in_str else None
            clock_out = datetime.strptime(clock_out_str, '%H:%M').time() if clock_out_str else None

            # Setup overtime and late
            overtime_minutes = 0
            late_minutes = 0

            weekday = date.weekday()  # 0 = Monday, 6 = Sunday

            if clock_in and clock_out:
                clock_in_dt = datetime.combine(date, clock_in)
                clock_out_dt = datetime.combine(date, clock_out)

                # Define working hours
                if weekday < 5:  # Mon-Fri
                    start_time = datetime.combine(date, time(10, 0))
                    grace_time = datetime.combine(date, time(10, 10))
                    end_time = datetime.combine(date, time(19, 0))
                elif weekday == 5:  # Saturday
                    start_time = datetime.combine(date, time(10, 0))
                    grace_time = datetime.combine(date, time(10, 10))
                    end_time = datetime.combine(date, time(15, 0))
                else:  # Sunday = full overtime
                    overtime_minutes = int((clock_out_dt - clock_in_dt).total_seconds() / 60)
                    start_time = end_time = grace_time = None  # Not needed
                if weekday < 6:
                    # Overtime before work start
                    if clock_in_dt < start_time:
                        overtime_minutes += int((start_time - clock_in_dt).total_seconds() / 60)

                    # Overtime after work end
                    if clock_out_dt > end_time:
                        overtime_minutes += int((clock_out_dt - end_time).total_seconds() / 60)

                    # Late after 10:10
                    if clock_in_dt > grace_time:
                        late_minutes = int((clock_in_dt - start_time).total_seconds() / 60)

            # Create attendance
            attendance = Attendance(
                user_id=user_id,
                date=date,
                clock_in=clock_in,
                clock_out=clock_out,
                overtime=overtime_minutes / 60.0,
                late_minutes=late_minutes
            )
            db.session.add(attendance)
            db.session.commit()
            flash("Attendance added successfully!", "success")
            return redirect(url_for('report'))

        except Exception as e:
            db.session.rollback()
            flash(f"Error adding attendance: {e}", "danger")

    users = User.query.all()
    return render_template('add_attendance.html', users=users)

@app.route('/attendance/delete/<int:user_id>/<string:date>', methods=['POST'])
@login_required
def delete_attendance(user_id, date):
    if current_user.role not in ['admin', 'manager']:
        flash("Access denied", "danger")
        return redirect(url_for('dashboard'))

    try:
        date_obj = datetime.strptime(date, '%Y-%m-%d').date()
        record = Attendance.query.filter_by(user_id=user_id, date=date_obj).first()
        if record:
            db.session.delete(record)
            db.session.commit()
            flash("Attendance deleted", "success")
    except Exception as e:
        flash(f"Error deleting attendance: {e}", "danger")

    return redirect(url_for('report'))

def calculate_attendance_metrics(date, clock_in, clock_out):
    weekday = date.weekday()  # Monday = 0, Sunday = 6
    late_minutes = 0
    overtime = 0.0

    if not clock_in or not clock_out:
        return 0.0, 0

    if weekday == 6:
        # Sunday - full duration = overtime
        duration = datetime.combine(date, clock_out) - datetime.combine(date, clock_in)
        overtime = duration.total_seconds() / 3600
        return round(overtime, 2), 0

    start_time = time(10, 0)
    grace_time = time(10, 10)
    end_time = time(19, 0) if weekday < 5 else time(15, 0)  # Mon–Fri or Sat

    if clock_in < start_time:
        early_seconds = (datetime.combine(date, start_time) - datetime.combine(date, clock_in)).total_seconds()
        overtime += early_seconds / 3600
    elif clock_in > grace_time:
        late_seconds = (datetime.combine(date, clock_in) - datetime.combine(date, grace_time)).total_seconds()
        late_minutes = int(late_seconds // 60)

    if clock_out > end_time:
        late_seconds = (datetime.combine(date, clock_out) - datetime.combine(date, end_time)).total_seconds()
        overtime += late_seconds / 3600

    return round(overtime, 2), late_minutes


@app.route('/qr-code')
@login_required
def qr_code():
    data = url_for('scan_qr', _external=True)
    qr_img = qrcode.make(data)
    buf = io.BytesIO()
    qr_img.save(buf, format='PNG')
    buf.seek(0)
    return send_file(buf, mimetype='image/png')

@api_bp.route('/scan')
def scan_page():
    token = request.args.get("token")
    
    if not token:
        return render_template("scan.html")  # just show QR if no token
    
    # Find the token in DB
    qr_entry = QRCodeToken.query.filter_by(token=token).first()
    
    if not qr_entry:
        flash("Invalid or expired QR token.", "danger")
        return render_template("scan.html")
    
    today = datetime.today().date()
    now = datetime.now()

    # Check if user already has attendance today
    attendance = Attendance.query.filter_by(user_id=current_user.id, date=today).first()
    
    if not attendance:
        # Clock in
        attendance = Attendance(
            user_id=current_user.id,
            date=today,
            clock_in=now.time()
        )
        db.session.add(attendance)
        flash("Clocked In successfully!", "success")
    elif not attendance.clock_out:
        # Clock out
        attendance.clock_out = now.time()
        flash("Clocked Out successfully!", "success")
    else:
        flash("You already clocked in and out today.", "info")
    
    # **Expire the token immediately**
    db.session.delete(qr_entry)
    
    db.session.commit()
    
    return render_template("scan.html", token=token)

@api_bp.route("/api/register-device", methods=["POST"])
def register_device():
    data = request.get_json()
    device_id = data.get("device_id")

    if not device_id:
        return jsonify({"error": "Missing device ID"}), 400

    response = make_response(jsonify({"status": "ok"}))
    response.set_cookie("device_id", device_id, max_age=60*60*24*365*5)  # 5 years
    return response

def get_expected_regular_hours(year, month):
    total = 0
    days_in_month = monthrange(year, month)[1]
    for day in range(1, days_in_month + 1):
        weekday = date(year, month, day).weekday()
        if weekday < 5:
            total += 9
        elif weekday == 5:
            total += 5
    return total    

@app.route('/deductions', methods=['GET', 'POST'])
@login_required
def manage_deductions():
    form = DeductionForm()
    form.user_id.choices = [(u.id, u.name) for u in User.query.order_by(User.name).all()]

    if form.validate_on_submit():
        deduction = Deduction(
            user_id=form.user_id.data,
            amount=form.amount.data,
            reason=form.reason.data,
            date=date.today()
        )
        db.session.add(deduction)
        db.session.commit()
        flash('Deduction added successfully!', 'success')
        return redirect(url_for('manage_deductions'))

    # Filtering
    start_date_str = request.args.get('start_date', date.today().isoformat())
    end_date_str = request.args.get('end_date', date.today().isoformat())
    user_id = request.args.get('user_id')

    start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
    end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()

    query = Deduction.query.filter(Deduction.date >= start_date, Deduction.date <= end_date)

    if current_user.role in ('manager',) or getattr(current_user, 'is_admin', False):
        if user_id:
            try:
                uid = int(user_id)
                query = query.filter(Deduction.user_id == uid)
            except ValueError:
                pass
        users = User.query.order_by(User.name).all()
    else:
        query = query.filter(Deduction.user_id == current_user.id)
        users = []

    deductions = query.order_by(Deduction.date.desc()).all()

    return render_template('deductions.html',
                           deductions=deductions,
                           users=users,
                           form=form,
                           start_date=start_date_str,
                           end_date=end_date_str,
                           currency="$",
                           selected_user_id=user_id or '')  

@app.route('/print_report/<int:user_id>/<string:month>')
@login_required
def print_report(user_id, month):
    user = User.query.get_or_404(user_id)
    year, month_num = map(int, month.split('-'))
    month_start = date(year, month_num, 1)
    month_end = date(year, month_num, monthrange(year, month_num)[1])

    # Format month nicely for display
    report_month_str = datetime(year, month_num, 1).strftime("%B %Y")  # e.g. "June 2025"

    attendances = Attendance.query.filter(
        Attendance.user_id == user_id,
        Attendance.date >= month_start,
        Attendance.date <= month_end,
    ).all()

    regular_hours = sum(att.regular_hours for att in attendances)
    overtime_hours = sum(att.overtime or 0 for att in attendances)
    total_hours = sum(att.total_hours for att in attendances)

    # Calculate total regular month hours
    total_regular_month_hours = 0
    for day_offset in range((month_end - month_start).days + 1):
        d = month_start + timedelta(days=day_offset)
        if d.weekday() < 5:
            total_regular_month_hours += 9
        elif d.weekday() == 5:
            total_regular_month_hours += 5

    salary = user.salary_per_month
    hourly_rate = salary / total_regular_month_hours if total_regular_month_hours > 0 else 0
    regular_pay = regular_hours * hourly_rate
    overtime_rate = hourly_rate * 1.5
    overtime_pay = overtime_hours * overtime_rate

    deductions = Deduction.query.filter(
        Deduction.user_id == user_id,
        Deduction.date >= month_start,
        Deduction.date <= month_end
    ).with_entities(func.coalesce(func.sum(Deduction.amount), 0)).scalar()

    advances = AdvanceSalary.query.filter(
        AdvanceSalary.user_id == user_id,
        AdvanceSalary.date >= month_start,
        AdvanceSalary.date <= month_end
    ).with_entities(func.coalesce(func.sum(AdvanceSalary.amount), 0)).scalar()

    total_salary = regular_pay + overtime_pay - deductions - advances
    working_salary = regular_pay + overtime_pay

    return render_template('print_monthly_report.html',
                           user=user,
                           fixed_salary=user.salary_per_month,
                           report_month=report_month_str,  # <-- pass formatted month string here
                           total_hours=total_hours,
                           regular_hours=regular_hours,
                           overtime_hours=overtime_hours,
                           hourly_rate=hourly_rate,
                           overtime_rate=overtime_rate,
                           regular_pay=regular_pay,
                           overtime_pay=overtime_pay,
                           deductions=deductions,
                           advances=advances,
                           total_salary=total_salary,
                           working_salary=working_salary,
                           currency="$"
                           )

@app.route('/export_report_excel')
def export_report_excel_monthly():
    start_date_str = request.args.get('start_date')
    end_date_str = request.args.get('end_date')
    user_id = request.args.get('user_id')

    if not start_date_str or not end_date_str:
        return "Start and end dates are required", 400

    try:
        start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
        end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
    except ValueError:
        return "Invalid date format, expected YYYY-MM-DD", 400

    # Query monthly aggregates same as in your report view
    query = db.session.query(
        func.strftime('%Y-%m', Attendance.date).label('month'),
        Attendance.user_id,
        func.count(func.distinct(Attendance.date)).label('work_days'),
        func.sum(Attendance.overtime).label('overtime')
    ).filter(
        Attendance.date >= start_date,
        Attendance.date <= end_date,
    )

    if user_id:
        try:
            uid = int(user_id)
            query = query.filter(Attendance.user_id == uid)
        except ValueError:
            pass
    else:
        if not (current_user.role in ('manager',) or getattr(current_user, 'is_admin', False)):
            query = query.filter(Attendance.user_id == current_user.id)

    monthly_raw = query.group_by('month', Attendance.user_id).all()

    user_ids = {row.user_id for row in monthly_raw}
    users = User.query.filter(User.id.in_(user_ids)).all()
    user_map = {u.id: u for u in users}

    data = []

    for row in monthly_raw:
        user_obj = user_map.get(row.user_id)
        salary = user_obj.salary_per_month if user_obj else 0
        year, month = map(int, str(row.month).split('-'))

        month_start = date(year, month, 1)
        days_in_month = monthrange(year, month)[1]
        month_end = date(year, month, days_in_month)

        attendances = Attendance.query.filter(
            Attendance.user_id == row.user_id,
            Attendance.date >= month_start,
            Attendance.date <= month_end,
        ).all()

        regular_hours_sum = sum(getattr(att, 'regular_hours', 0) for att in attendances)
        overtime_hours = sum(att.overtime or 0 for att in attendances)
        total_hours_sum = sum(getattr(att, 'total_hours', 0) for att in attendances)

        total_regular_month_hours = 0
        for day_offset in range(days_in_month):
            day = month_start + timedelta(days=day_offset)
            if day.weekday() < 5:
                total_regular_month_hours += 9
            elif day.weekday() == 5:
                total_regular_month_hours += 5

        hourly_rate = salary / total_regular_month_hours if total_regular_month_hours > 0 else 0
        regular_pay = regular_hours_sum * hourly_rate
        overtime_rate = hourly_rate * 1.5
        overtime_payment = overtime_hours * overtime_rate

        deductions = Deduction.query.filter(
            Deduction.user_id == row.user_id,
            Deduction.date >= month_start,
            Deduction.date <= month_end
        ).with_entities(func.coalesce(func.sum(Deduction.amount), 0)).scalar() or 0

        advances = AdvanceSalary.query.filter(
            AdvanceSalary.user_id == row.user_id,
            AdvanceSalary.date >= month_start,
            AdvanceSalary.date <= month_end
        ).with_entities(func.coalesce(func.sum(AdvanceSalary.amount), 0)).scalar() or 0

        total_salary = regular_pay + overtime_payment - deductions - advances
        working_salary = regular_pay + overtime_payment

        data.append({
            'Month': row.month,
            'Employee': user_obj.name if user_obj else f"User ID {row.user_id}",
            'Total Work Days': row.work_days,
            'Total Hours': round(total_hours_sum, 2),
            'Regular Hours': round(regular_hours_sum, 2),
            'Hourly Rate': round(hourly_rate, 2),
            'Regular Pay': round(regular_pay, 2),
            'Overtime': round(overtime_hours, 2),
            'Overtime Rate': round(overtime_rate, 2),
            'Overtime Pay': round(overtime_payment, 2),
            'Working Salary': round(working_salary, 2),
            'Deductions': round(deductions, 2),
            'Advanced Payments': round(advances, 2),
            'Total Salary': round(total_salary, 2),
        })

    if not data:
        return "No monthly data found for the given filters.", 404

    df = pd.DataFrame(data)

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        workbook = writer.book
        worksheet = workbook.add_worksheet('Monthly Report')
        writer.sheets['Monthly Report'] = worksheet

        # Summary table
        total_employees = len(set(row['Employee'] for row in data))
        total_salary_sum = sum(row['Total Salary'] for row in data)

        header_format = workbook.add_format({'bold': True, 'font_color': 'blue', 'font_size': 12})
        currency_format = workbook.add_format({'num_format': '$#,##0.00'})
        normal_format = workbook.add_format()

        worksheet.write('A1', 'Summary', header_format)
        worksheet.write('A2', 'Total Employees', normal_format)
        worksheet.write('B2', total_employees, normal_format)
        worksheet.write('A3', 'Sum of Total Salary', normal_format)
        worksheet.write('B3', total_salary_sum, currency_format)

        # Write main dataframe starting at row 5
        df.to_excel(writer, sheet_name='Monthly Report', startrow=5, index=False)

        # Currency formatting columns
        # Columns are zero-indexed in Excel; map your dataframe columns:
        # ['Month', 'Employee', 'Total Work Days', 'Total Hours', 'Regular Hours',
        # 'Hourly Rate', 'Regular Pay', 'Overtime', 'Overtime Rate',
        # 'Overtime Pay', 'Working Salary', 'Deductions', 'Advanced Payments', 'Total Salary']
        currency_cols = [5, 6, 8, 9, 10, 11, 12, 13]

        for col_idx in currency_cols:
            worksheet.set_column(col_idx, col_idx, 15, currency_format)

        # Adjust widths for other columns
        for i, col in enumerate(df.columns):
            if i not in currency_cols:
                max_len = max(df[col].astype(str).map(len).max(), len(col)) + 2
                worksheet.set_column(i, i, max_len)

    output.seek(0)

    return send_file(
        output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name='monthly_report.xlsx'
    )

@app.route('/start_break', methods=['POST'])
@login_required
def start_break():
    today = date.today()
    now_time = datetime.now().time()

    # Check if user already on a break today with no end time
    active_break = BreakSession.query.filter_by(user_id=current_user.id, date=today, end_time=None).first()
    if active_break:
        flash("You are already on a break!", "warning")
        return redirect(url_for('dashboard'))

    # Create new break session
    new_break = BreakSession(user_id=current_user.id, date=today, start_time=now_time, end_time=None)
    db.session.add(new_break)
    db.session.commit()

    flash("Break started", "success")
    return redirect(url_for('dashboard'))

@app.route('/end_break', methods=['POST'])
@login_required
def end_break():
    today = date.today()
    break_session = BreakSession.query.filter_by(
        user_id=current_user.id,
        date=today,
        end_time=None
    ).first()

    if break_session:
        break_session.end_time = datetime.now().time()
        db.session.commit()
        flash('Break ended successfully.', 'success')
    else:
        flash('No active break to end.', 'warning')

    return redirect(url_for('dashboard'))

@app.route('/break_reports')
@login_required
def break_reports():
    if current_user.role != 'manager':
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard'))

    current_date = date.today().strftime('%Y-%m-%d')
    start_date = request.args.get('start_date') or current_date
    end_date = request.args.get('end_date') or current_date
    employee_id = request.args.get('employee_id')

    query = BreakSession.query.join(User).filter(User.role == 'employee')

    if start_date:
        query = query.filter(BreakSession.date >= start_date)
    if end_date:
        query = query.filter(BreakSession.date <= end_date)
    if employee_id:
        query = query.filter(BreakSession.user_id == employee_id)

    raw_breaks = query.order_by(BreakSession.date.desc()).all()

    breaks = []
    for b in raw_breaks:
        if b.start_time and b.end_time:
            duration = (datetime.combine(date.today(), b.end_time) - datetime.combine(date.today(), b.start_time)).total_seconds() / 60
        else:
            duration = 0
        deduction = max(0, duration - 60)
        breaks.append({
            'user': b.user,
            'date': b.date,
            'start_time': b.start_time,
            'end_time': b.end_time,
            'duration': int(duration),
            'deduction': int(deduction)
        })

    employees = User.query.filter_by(role='employee').all()

    return render_template(
        'break_reports.html',
        breaks=breaks,
        employees=employees,
        current_date=current_date
    )

UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'xlsx'}

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route('/import_attendance', methods=['GET', 'POST'])
@login_required
def import_attendance():
    if current_user.role != 'manager':
        flash('Access denied', 'danger')
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        file = request.files.get('file')
        if not file or not allowed_file(file.filename):
            flash('Please upload a valid Excel (.xlsx) file.', 'danger')
            return redirect(request.url)

        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
        file.save(filepath)

        try:
            df = pd.read_excel(filepath)

            count = 0  # Start counter

            for index, row in df.iterrows():
                username = str(row['username']).strip()
                user = User.query.filter_by(username=username).first()
                if not user:
                    continue  # Skip rows with unknown users

                att_date = pd.to_datetime(row['date']).date()
                clock_in = datetime.strptime(str(row['clock_in']), '%H:%M:%S').time() if not pd.isna(row['clock_in']) else None
                clock_out = datetime.strptime(str(row['clock_out']), '%H:%M:%S').time() if not pd.isna(row['clock_out']) else None

                overtime, late_minutes = calculate_attendance_metrics(att_date, clock_in, clock_out)

                attendance = Attendance(
                    user_id=user.id,
                    date=att_date,
                    clock_in=clock_in,
                    clock_out=clock_out,
                    overtime=overtime,
                    late_minutes=late_minutes
            )
                db.session.add(attendance)
                count += 1

            db.session.commit()
            flash(f'{count} attendance record(s) imported successfully!', 'success')
            return redirect(url_for('daily_report'))

        except Exception as e:
            flash(f'Import failed: {str(e)}', 'danger')
            return redirect(request.url)

    return render_template('import_attendance.html')

@app.route('/download-template')
@login_required
def download_template():
    if current_user.role != 'manager':
        flash('Access denied.')
        return redirect(url_for('dashboard'))

    data = {
        'username': ['example_user'],
        'date': ['2025-06-25'],
        'clock_in': ['09:00:00'],
        'clock_out': ['18:00:00']
    }
    df = pd.DataFrame(data)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False)
    output.seek(0)

    return send_file(output, download_name='attendance_import_template.xlsx', as_attachment=True)

def on_connect(tag):
    uid = tag.identifier.hex()
    print(f"NFC Tag detected UID: {uid}")

    # Send UID to your Flask API endpoint
    response = requests.post('http://localhost:5000/api/clock', json={'uid': uid})
    if response.ok:
        print("Clock-in/out successful!")
    else:
        print("Error in clock-in/out.")

    return True

def nfc_scan():
    data = request.json
    nfc_uid = data.get('nfc_uid', '').strip()
    if not nfc_uid:
        return jsonify({'error': 'No NFC UID provided'}), 400
    
    user = User.query.filter_by(nfc_uid=nfc_uid).first()
    if not user:
        return jsonify({'error': 'No user found with this NFC UID'}), 404
    
    # Check if user already clocked in today without clocking out
    today = datetime.now().date()
    attendance = Attendance.query.filter_by(user_id=user.id, date=today, clock_out=None).first()

    now = datetime.now()
    if attendance:
        # User is clocking out now
        attendance.clock_out = now
        status = 'clocked_out'
    else:
        # User is clocking in now - create new attendance record
        attendance = Attendance(user_id=user.id, date=today, clock_in=now)
        db.session.add(attendance)
        status = 'clocked_in'
    
    db.session.commit()
    return jsonify({
        'status': status,
        'user': user.name,
        'time': now.strftime("%Y-%m-%d %H:%M:%S")
    })


@api_bp.route('/attendance/scan', methods=['GET', 'POST'])
def attendance_scan():
    message = "Please scan your NFC card."

    if request.method == 'POST':
        serial_number = request.form.get('serial_number')

        if not serial_number:
            message = "No serial number received, please try again."
        else:
            user = User.query.filter_by(serial_number=serial_number).first()

            if not user:
                message = f"Unknown card serial number: {serial_number}"
            else:
                now = datetime.now()
                today_start = datetime.combine(now.date(), datetime.min.time())  # midnight today

                attendance = Attendance.query.filter(
                    Attendance.user_id == user.id,
                    Attendance.date == now.date()
                ).order_by(Attendance.clock_in.desc()).first()

                if not attendance:
                    # Clock in
                    new_attendance = Attendance(
                        user_id=user.id,
                        date=now.date(),        # Store date separately
                        clock_in=now.time(),    # Store only time part for clock_in
                        clock_out=None
                    )
                    db.session.add(new_attendance)
                    db.session.commit()
                    message = f"✅ Welcome {user.name}, you clocked in at {now.strftime('%H:%M:%S')}."
                elif attendance.clock_out is None:
                    # Clock out
                    attendance.clock_out = now.time()  # Store only time part for clock_out
                    db.session.commit()
                    # Calculate worked duration by combining date and time for clock_in/out
                    clock_in_datetime = datetime.combine(attendance.date, attendance.clock_in)
                    clock_out_datetime = datetime.combine(attendance.date, attendance.clock_out)
                    duration = clock_out_datetime - clock_in_datetime
                    hours, remainder = divmod(duration.total_seconds(), 3600)
                    minutes, _ = divmod(remainder, 60)
                    message = f"👋 Goodbye {user.name}, you worked {int(hours)}h {int(minutes)}m today."
                else:
                    message = f"⏱️ {user.name}, you already clocked in and out today."

    return render_template('attendance_scan.html', message=message)
    
@app.route('/attendance/detail/<int:user_id>')
def attendance_detail(user_id):
    user = User.query.get_or_404(user_id)
    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    attendance = Attendance.query.filter(
        Attendance.user_id == user.id,
        Attendance.clock_in >= today_start
    ).order_by(Attendance.clock_in.desc()).first()

    if not attendance:
        message = f"No attendance record found for today, {user.name}."
        return render_template('attendance_detail.html', message=message)

    if attendance.clock_out is None:
        message = f"Welcome {user.name}, you clocked in at {attendance.clock_in.strftime('%H:%M:%S')}."
    else:
        duration = attendance.clock_out - attendance.clock_in
        hours, remainder = divmod(duration.total_seconds(), 3600)
        minutes, _ = divmod(remainder, 60)
        message = f"Goodbye {user.name}, you worked {int(hours)}h {int(minutes)}m today."

    return render_template('attendance_detail.html', message=message)   

@app.route("/admin/assign-device/<int:user_id>", methods=["POST"])
@login_required
def assign_device(user_id):
    # Get device_id from cookie (employee must scan QR first)
    device_id = request.cookies.get("device_id")

    if not device_id:
        flash("No device detected. Ask the employee to scan the QR first.", "danger")
        return redirect(url_for("api.manage_devices"))


    user = User.query.get_or_404(user_id)

    if user.device_id:
        flash("This employee already has a registered device.", "warning")
        return redirect(url_for("api.manage_devices"))


    user.device_id = device_id
    db.session.commit()

    flash(f"Device successfully assigned to {user.username}.", "success")
    return redirect(url_for("api.manage_devices"))


@app.route('/import-advance-salaries', methods=['POST'])
def import_advance_salaries():
    file = request.files.get('excel_file')
    if not file:
        flash("No file uploaded.", "danger")
        return redirect(url_for('add_advance'))

    try:
        df = pd.read_excel(file)

        for index, row in df.iterrows():
            username = str(row['username']).strip()
            user = User.query.filter_by(username=username).first()
            if not user:
                flash(f"User '{username}' not found in row {index + 2}", "warning")
                continue

            amount = float(row['amount'])
            date = pd.to_datetime(row['date']).date()

            advance = AdvanceSalary(user_id=user.id, amount=amount, date=date)
            db.session.add(advance)

        db.session.commit()
        flash("Advance salaries imported successfully.", "success")

    except Exception as e:
        flash(f"Error importing file: {str(e)}", "danger")
        return redirect(url_for('add_advance'))

    return redirect(url_for('add_advance'))


@app.route('/download-advance-template')
def download_advance_template():
    df = pd.DataFrame([{
        'username': 'john_doe',
        'amount': 150.00,
        'date': '2025-07-31'
    }])

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df.to_excel(writer, index=False, sheet_name='AdvanceSalaries')
    output.seek(0)

    return send_file(
        output,
        download_name='advance_salary_template.xlsx',
        as_attachment=True,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )

@app.route('/daily-report-print')
@login_required
def daily_report_print():
    start_date = request.args.get('start_date')
    user_id = request.args.get('user_id')

    try:
        date = datetime.strptime(start_date, '%Y-%m-%d') if start_date else datetime.today()
    except Exception:
        flash("Invalid date format.", "danger")
        return redirect(url_for('daily_report'))

    daily_data = get_daily_attendance_data(date, user_id)

    return render_template('daily_report_pdf.html', daily_data=daily_data, report_date=date.strftime('%Y-%m-%d'))


def get_daily_attendance_data(date, user_id=None):
    query = Attendance.query.filter(Attendance.date == date)

    if user_id:
        query = query.filter(Attendance.user_id == user_id)

    records = query.order_by(Attendance.clock_in).all()

    data = []
    for record in records:
        user = record.user
        data.append({
            'username': user.username,
            'name': user.name,
            'date': record.date.strftime('%Y-%m-%d'),
            'clock_in': record.clock_in.strftime('%H:%M') if record.clock_in else '',
            'clock_out': record.clock_out.strftime('%H:%M') if record.clock_out else '',
            'regular_hours': record.regular_hours or 0,
            'overtime_hours': record.overtime_hours or 0,
            'late_minutes': record.late_minutes or 0
        })

    return data

@app.route('/daily-report-pdf')
@login_required
def daily_report_pdf():
    start_date = request.args.get('start_date')
    user_id = request.args.get('user_id')
    try:
        date = datetime.strptime(start_date, '%Y-%m-%d') if start_date else datetime.today()
    except Exception:
        flash("Invalid date format.", "danger")
        return redirect(url_for('daily_report'))

    daily_data = get_daily_attendance_data(date, user_id)
    html = render_template('daily_report_pdf.html', daily_data=daily_data, report_date=date.strftime('%Y-%m-%d'))

    # Generate PDF
    pdf = HTML(string=html).write_pdf()
    response = make_response(pdf)
    response.headers['Content-Type'] = 'application/pdf'
    response.headers['Content-Disposition'] = f'attachment; filename=daily_report_{date.strftime("%Y-%m-%d")}.pdf'
    return response

@app.route('/daily-report-excel')
@login_required
def daily_report_excel():
    start_date = request.args.get('start_date')
    user_id = request.args.get('user_id')
    try:
        date = datetime.strptime(start_date, '%Y-%m-%d') if start_date else datetime.today()
    except Exception:
        flash("Invalid date format.", "danger")
        return redirect(url_for('daily_report'))

    daily_data = get_daily_attendance_data(date, user_id)

    # Create DataFrame
    df = pd.DataFrame(daily_data)

    # Convert DataFrame to Excel in-memory
    from io import BytesIO
    output = BytesIO()
    writer = pd.ExcelWriter(output, engine='openpyxl')
    df.to_excel(writer, index=False, sheet_name='Daily Report')
    writer.close()
    output.seek(0)

    # Send response
    response = make_response(output.read())
    response.headers['Content-Disposition'] = f'attachment; filename=daily_report_{date.strftime("%Y-%m-%d")}.xlsx'
    response.headers['Content-Type'] = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    return response

@app.route("/admin/live-qr")
@login_required
def live_qr():
    """
    Returns a unique token for QR code generation.
    This token can be used to identify a clock-in/out session.
    """
    token = secrets.token_urlsafe(16)  # Random unique token
    return jsonify({"token": token})

@app.route("/admin/qr")
@login_required
def show_admin_qr():
    return render_template("admin/live_qr.html")

@app.route("/qr/clock")
def qr_clock():
    token = request.args.get("token")

    qr = QRCodeToken.query.filter_by(token=token, used=False).first()

    if not qr:
        return "Invalid or already used QR", 403

    if datetime.utcnow() - qr.created_at > timedelta(seconds=15):
        return "QR expired", 403

    qr.used = True
    db.session.commit()

    # 🔽 YOUR EXISTING LOGIC GOES HERE
    # Identify employee (device ID / login)
    # Clock in or clock out

    return render_template("employee/qr_result.html")

@app.route("/admin/live-qr")
@login_required
def admin_live_qr():
    # Generate a unique token
    token = secrets.token_urlsafe(32)

    # Save token in DB
    qr = QRCodeToken(token=token)
    db.session.add(qr)
    db.session.commit()

    # Generate QR image
    qr_data = f"http://127.0.0.1:5000/qr/clock?token={token}"
    img = qrcode.make(qr_data)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)

    # Serve image directly
    return send_file(buf, mimetype='image/png')

@api_bp.route('/qr-image')
def qr_image():
    # Generate a new unique token
    token = secrets.token_urlsafe(16)
    
    # Save token to DB
    qr = QRCodeToken(token=token, used=False)
    db.session.add(qr)
    db.session.commit()
    
    # Full URL that QR will point to
    #full_url = f"https://attendance-64n0.onrender.com/scan?token={token}"#
    full_url = url_for("api.scan_action", token=token, _external=True)
    
    # Generate QR code
    qr_img = qrcode.make(full_url)
    
    # Save to memory buffer
    img_io = io.BytesIO()
    qr_img.save(img_io, 'PNG')
    img_io.seek(0)
    
    return send_file(img_io, mimetype='image/png')

@api_bp.route('/scan-action')
def scan_action():
    token = request.args.get("token")

    qr_entry = QRCodeToken.query.filter_by(token=token, used=False).first()
    if not qr_entry:
        return "Invalid or expired QR code", 400

    # Expire token immediately
    qr_entry.used = True
    db.session.commit()

    device_id = request.cookies.get("device_id")
    if not device_id:
        return redirect(url_for("register_device"))

    employee = User.query.filter_by(device_id=device_id).first()
    if not employee:
        return redirect(url_for("register_device"))

    return process_attendance(employee)

def process_attendance(employee):
    today = date.today()
    now = datetime.now().time()

    attendance = Attendance.query.filter_by(
        user_id=employee.id,
        date=today
    ).first()

    if not attendance:
        attendance = Attendance(
            user_id=employee.id,
            date=today,
            clock_in=now
        )
        db.session.add(attendance)
        db.session.commit()
        return "Clocked In ✅"

    elif not attendance.clock_out:
        attendance.clock_out = now
        db.session.commit()
        return "Clocked Out ✅"

    else:
        return "Already clocked in and out today ℹ️"
    
@app.route("/admin/reset-device/<int:user_id>", methods=["POST"])
@login_required
def reset_device(user_id):
    user = User.query.get_or_404(user_id)
    user.device_id = None
    db.session.commit()
    flash("Device reset. Employee must re-scan.", "success")
    return redirect(url_for("api.manage_devices"))
    

def admin_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not getattr(current_user, "is_admin", False):
            abort(403)
        return func(*args, **kwargs)
    return wrapper

# Show list of employees and device IDs
@api_bp.route("/admin/devices")
@login_required
@admin_required
def manage_devices():
    employees = User.query.all()
    return render_template("admin_devices.html", employees=employees)

@app.route("/live-qr-token")
def public_live_qr():
    token = secrets.token_urlsafe(16)
    return jsonify({"token": token})