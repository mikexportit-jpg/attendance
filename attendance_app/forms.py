from flask_wtf import FlaskForm
from wtforms import SelectField, DecimalField, StringField, SubmitField
from wtforms.validators import DataRequired, NumberRange
from attendance_app.models import User

class DeductionForm(FlaskForm):
    user_id = SelectField('Employee', coerce=int)
    amount = DecimalField('Amount', validators=[DataRequired()])
    reason = StringField('Reason')
    submit = SubmitField('Add Deduction')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.user_id.choices = [(u.id, u.name) for u in User.query.order_by(User.name).all()]
