from flask import Flask, render_template_string, request, redirect, url_for, flash, session
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta
import uuid
import hashlib
import random
import string
from functools import wraps
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import os
import requests
from werkzeug.utils import secure_filename
import base64
from io import BytesIO
from PIL import Image

app = Flask(__name__)
app.config['SECRET_KEY'] = 'supersecretkeyforexontogram2026'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///exontogram.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# НАСТРОЙКИ ЗАГРУЗКИ ФАЙЛОВ
app.config['UPLOAD_FOLDER'] = 'static/uploads'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max
app.config['ALLOWED_EXTENSIONS'] = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

# НАСТРОЙКИ СЖАТИЯ ИЗОБРАЖЕНИЙ
app.config['MAX_IMAGE_SIZE'] = (800, 800)  # Максимальный размер для загруженных изображений
app.config['AVATAR_SIZE'] = (200, 200)     # Размер аватарок
app.config['THUMBNAIL_SIZE'] = (400, 400)  # Размер превью для постов
app.config['IMAGE_QUALITY'] = 85            # Качество сжатия JPEG (1-100)

# НАСТРОЙКИ ПОЧТЫ
app.config['MAIL_SERVER'] = 'smtp.mail.ru'
app.config['MAIL_PORT'] = 465
app.config['MAIL_USE_SSL'] = True
app.config['MAIL_USERNAME'] = 'efmstudio@inbox.ru'
app.config['MAIL_PASSWORD'] = 'TNYIFhKVKzEyiQ4GSXx5'

# АВАТАР ПО УМОЛЧАНИЮ (1280x1280)
DEFAULT_AVATAR_URL = "https://i.pinimg.com/originals/65/1c/6d/651c6da502353948bdc929f02da2b8e0.jpg?nii=t"

# API для случайных котов
CAT_API_URL = "https://api.thecatapi.com/v1/images/search"

# Создаем папку для загрузок, если её нет
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

db = SQLAlchemy(app)

# ==================== МОДЕЛИ ДАННЫХ ====================

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    efm_id = db.Column(db.String(100), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    display_name = db.Column(db.String(100))
    password_hash = db.Column(db.String(200))
    avatar_url = db.Column(db.String(500), default=DEFAULT_AVATAR_URL)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_banned = db.Column(db.Boolean, default=False)
    is_admin = db.Column(db.Boolean, default=False)
    is_verified = db.Column(db.Boolean, default=False)
    verification_code = db.Column(db.String(6), nullable=True)
    verification_expires = db.Column(db.DateTime, nullable=True)
    banned_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    banned_at = db.Column(db.DateTime, nullable=True)
    
    posts = db.relationship('Post', backref='author', lazy=True, cascade='all, delete-orphan')
    likes = db.relationship('Like', backref='user', lazy=True, cascade='all, delete-orphan')
    comments = db.relationship('Comment', backref='author', lazy=True, cascade='all, delete-orphan')
    
    def check_password(self, password):
        return self.password_hash == hashlib.sha256(password.encode()).hexdigest()
    
    def set_password(self, password):
        self.password_hash = hashlib.sha256(password.encode()).hexdigest()
    
    def generate_verification_code(self):
        self.verification_code = ''.join(random.choices(string.digits, k=6))
        self.verification_expires = datetime.utcnow() + timedelta(minutes=15)
        return self.verification_code

class Post(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    uuid = db.Column(db.String(50), unique=True, default=lambda: str(uuid.uuid4()))
    content = db.Column(db.Text, nullable=False)
    media_url = db.Column(db.String(500))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    is_echo = db.Column(db.Boolean, default=False)
    echo_expires_at = db.Column(db.DateTime)
    echo_survived = db.Column(db.Boolean, default=False)
    
    likes_count = db.Column(db.Integer, default=0)
    comments_count = db.Column(db.Integer, default=0)
    
    likes = db.relationship('Like', backref='post', lazy=True, cascade='all, delete-orphan')
    comments = db.relationship('Comment', backref='post', lazy=True, cascade='all, delete-orphan')

class Like(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    post_id = db.Column(db.Integer, db.ForeignKey('post.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    __table_args__ = (db.UniqueConstraint('user_id', 'post_id', name='unique_like'),)

class Comment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    content = db.Column(db.Text, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    post_id = db.Column(db.Integer, db.ForeignKey('post.id'), nullable=False)
    parent_id = db.Column(db.Integer, db.ForeignKey('comment.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    replies = db.relationship('Comment', backref=db.backref('parent', remote_side=[id]), lazy=True, cascade='all, delete-orphan')

class BanLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    admin_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    banned_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    reason = db.Column(db.String(500))
    banned_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    admin = db.relationship('User', foreign_keys=[admin_id])
    banned_user = db.relationship('User', foreign_keys=[banned_user_id])

# ==================== ФУНКЦИИ ДЛЯ РАБОТЫ С ИЗОБРАЖЕНИЯМИ ====================

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

def resize_image(input_path, output_path, max_size, quality=85):
    """Изменяет размер изображения с сохранением пропорций"""
    try:
        with Image.open(input_path) as img:
            # Конвертируем в RGB если нужно (для JPEG)
            if img.mode in ('RGBA', 'LA', 'P'):
                bg = Image.new('RGB', img.size, (255, 255, 255))
                if img.mode == 'P':
                    img = img.convert('RGBA')
                bg.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
                img = bg
            
            # Изменяем размер с сохранением пропорций
            img.thumbnail(max_size, Image.Resampling.LANCZOS)
            
            # Сохраняем с оптимизацией
            img.save(output_path, 'JPEG', quality=quality, optimize=True)
            return True
    except Exception as e:
        print(f"Ошибка при изменении размера изображения: {e}")
        return False

def process_and_save_image(file, max_size, quality=85):
    """Обрабатывает и сохраняет загруженное изображение"""
    try:
        # Генерируем уникальное имя файла
        ext = file.filename.rsplit('.', 1)[1].lower()
        filename = f"{uuid.uuid4()}.jpg"  # Всегда сохраняем как JPEG
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        
        # Временное сохранение
        temp_path = filepath + '.temp'
        file.save(temp_path)
        
        # Изменяем размер и сохраняем
        if resize_image(temp_path, filepath, max_size, quality):
            os.remove(temp_path)  # Удаляем временный файл
            return f'/static/uploads/{filename}'
        else:
            os.remove(temp_path)
            return None
    except Exception as e:
        print(f"Ошибка обработки изображения: {e}")
        return None

def save_base64_image(base64_string, image_type='post'):
    """Сохраняет base64 изображение с изменением размера"""
    try:
        # Удаляем префикс data:image/xxx;base64,
        if 'base64,' in base64_string:
            base64_string = base64_string.split('base64,')[1]
        
        # Декодируем base64
        image_data = base64.b64decode(base64_string)
        
        # Выбираем размер в зависимости от типа
        max_size = app.config['AVATAR_SIZE'] if image_type == 'avatar' else app.config['THUMBNAIL_SIZE']
        
        # Генерируем уникальное имя файла
        filename = f"{uuid.uuid4()}.jpg"
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        
        # Сохраняем временно
        temp_path = filepath + '.temp'
        with open(temp_path, 'wb') as f:
            f.write(image_data)
        
        # Изменяем размер и сохраняем
        if resize_image(temp_path, filepath, max_size, app.config['IMAGE_QUALITY']):
            os.remove(temp_path)
            return f'/static/uploads/{filename}'
        else:
            os.remove(temp_path)
            return None
    except Exception as e:
        print(f"Ошибка сохранения изображения: {e}")
        return None

def get_random_cat():
    """Получает случайного кота из API"""
    try:
        response = requests.get(CAT_API_URL, timeout=5)
        if response.status_code == 200:
            data = response.json()
            return data[0]['url']
    except:
        pass
    return "https://cdn2.thecatapi.com/images/MTYzOTU0Mg.jpg"  # Запасной кот

# ==================== ФУНКЦИИ ОТПРАВКИ ПИСЕМ ====================

def send_verification_email(user_email, code):
    sender = app.config['MAIL_USERNAME']
    password = app.config['MAIL_PASSWORD']
    
    subject = "Код подтверждения для EFM ID"
    body = f"""
    Здравствуйте!
    
    Ваш код подтверждения для регистрации в системе EFM ID: {code}
    
    Код действителен в течение 15 минут.
    
    Если вы не регистрировались в EFM ID, просто проигнорируйте это письмо.
    
    С уважением,
    Команда EFM ID
    """
    
    msg = MIMEMultipart()
    msg['From'] = sender
    msg['To'] = user_email
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'plain', 'utf-8'))
    
    try:
        server = smtplib.SMTP_SSL(app.config['MAIL_SERVER'], app.config['MAIL_PORT'])
        server.login(sender, password)
        server.send_message(msg)
        server.quit()
        print(f"✅ Код подтверждения отправлен на {user_email}")
        return True
    except Exception as e:
        print(f"❌ Ошибка при отправке письма: {e}")
        return False

def send_account_deletion_email(user_email, efm_id):
    sender = app.config['MAIL_USERNAME']
    password = app.config['MAIL_PASSWORD']
    
    subject = "Аккаунт EFM ID удален"
    body = f"""
    Здравствуйте!
    
    Ваш аккаунт с EFM ID {efm_id} был успешно удален из системы EFM ID.
    
    Если вы не совершали это действие, немедленно свяжитесь с администрацией.
    
    С уважением,
    Команда EFM ID
    """
    
    msg = MIMEMultipart()
    msg['From'] = sender
    msg['To'] = user_email
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'plain', 'utf-8'))
    
    try:
        server = smtplib.SMTP_SSL(app.config['MAIL_SERVER'], app.config['MAIL_PORT'])
        server.login(sender, password)
        server.send_message(msg)
        server.quit()
        print(f"✅ Уведомление об удалении отправлено на {user_email}")
        return True
    except Exception as e:
        print(f"❌ Ошибка при отправке письма: {e}")
        return False

# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Пожалуйста, войдите в систему', 'danger')
            return redirect(url_for('login'))
        
        user = User.query.get(session['user_id'])
        if user and user.is_banned:
            session.clear()
            flash('Ваш аккаунт забанен', 'danger')
            return redirect(url_for('login'))
        
        if user and not user.is_verified:
            flash('Пожалуйста, подтвердите email', 'warning')
            return redirect(url_for('verify'))
        
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Пожалуйста, войдите в систему', 'danger')
            return redirect(url_for('login'))
        
        user = User.query.get(session['user_id'])
        if not user or not user.is_admin:
            flash('Доступ запрещен. Требуются права администратора', 'danger')
            return redirect(url_for('index'))
        
        if user.is_banned:
            session.clear()
            flash('Ваш аккаунт забанен', 'danger')
            return redirect(url_for('login'))
        
        if not user.is_verified:
            flash('Пожалуйста, подтвердите email', 'warning')
            return redirect(url_for('verify'))
        
        return f(*args, **kwargs)
    return decorated_function

def get_current_user():
    if 'user_id' in session:
        user = User.query.get(session['user_id'])
        if user and not user.is_banned:
            return user
        elif user and user.is_banned:
            session.clear()
    return None

def check_echo_posts():
    expired_echoes = Post.query.filter(
        Post.is_echo == True,
        Post.echo_survived == False,
        Post.echo_expires_at < datetime.utcnow()
    ).all()
    
    for post in expired_echoes:
        if post.likes_count < 100:
            db.session.delete(post)
        else:
            post.echo_survived = True
            post.is_echo = False
    
    db.session.commit()

def process_post_content(content):
    """Обрабатывает контент поста, ищет теги и заменяет их"""
    if '<cat>' in content:
        # Заменяем тег на URL кота
        cat_url = get_random_cat()
        content = content.replace('<cat>', '')
        return content, cat_url
    return content, None

def create_admin():
    """Создает администратора при первом запуске"""
    admin = User.query.filter_by(efm_id='admin').first()
    if not admin:
        admin = User(
            efm_id='admin',
            email='efmstudio@inbox.ru',
            display_name='Administrator',
            is_admin=True,
            is_verified=True,
            avatar_url=DEFAULT_AVATAR_URL
        )
        admin.set_password('fima1456Game!')
        db.session.add(admin)
        db.session.commit()
        print('✅ Администратор создан: admin / fima1456Game!')
        print('✅ Входить можно: admin ИЛИ efmstudio@inbox.ru')
    else:
        print('ℹ️ Администратор уже существует')

# ==================== МАРШРУТЫ ====================

@app.route('/')
def index():
    user = get_current_user()
    check_echo_posts()
    
    posts = Post.query.join(User).filter(
        User.is_banned == False,
        User.is_verified == True
    ).order_by(Post.created_at.desc()).all()
    
    return render_template_string(HTML_TEMPLATE, user=user, posts=posts, now=datetime.utcnow())

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        efm_id = request.form['efm_id'].strip().lower()
        email = request.form['email'].strip().lower()
        display_name = request.form['display_name'].strip()
        password = request.form['password']
        
        if not efm_id or not email or not display_name or not password:
            flash('Все поля обязательны для заполнения', 'danger')
            return redirect(url_for('register'))
        
        # Проверка на занятость
        existing_user = User.query.filter_by(efm_id=efm_id).first()
        if existing_user:
            flash(f'EFM ID "{efm_id}" уже занят. Попробуйте другое имя', 'danger')
            return redirect(url_for('register'))
        
        existing_email = User.query.filter_by(email=email).first()
        if existing_email:
            flash('Email уже зарегистрирован', 'danger')
            return redirect(url_for('register'))
        
        # Создаем пользователя
        user = User(
            efm_id=efm_id,
            email=email,
            display_name=display_name,
            is_verified=False,
            avatar_url=DEFAULT_AVATAR_URL
        )
        user.set_password(password)
        
        verification_code = user.generate_verification_code()
        
        db.session.add(user)
        db.session.commit()
        
        if send_verification_email(email, verification_code):
            session['verification_user_id'] = user.id
            flash(f'Код подтверждения отправлен на {email}', 'info')
            return redirect(url_for('verify'))
        else:
            db.session.delete(user)
            db.session.commit()
            flash('Ошибка при отправке письма. Попробуйте позже', 'danger')
            return redirect(url_for('register'))
    
    return render_template_string(HTML_TEMPLATE, user=None)

@app.route('/verify', methods=['GET', 'POST'])
def verify():
    if 'verification_user_id' not in session:
        flash('Сначала зарегистрируйтесь', 'warning')
        return redirect(url_for('register'))
    
    user = User.query.get(session['verification_user_id'])
    if not user:
        session.pop('verification_user_id', None)
        flash('Ошибка верификации', 'danger')
        return redirect(url_for('register'))
    
    if request.method == 'POST':
        code = request.form['code'].strip()
        
        if (user.verification_code == code and 
            user.verification_expires and 
            user.verification_expires > datetime.utcnow()):
            
            user.is_verified = True
            user.verification_code = None
            user.verification_expires = None
            db.session.commit()
            
            session.pop('verification_user_id', None)
            flash('Email подтвержден! Теперь вы можете войти', 'success')
            return redirect(url_for('login'))
        else:
            flash('Неверный или истекший код', 'danger')
    
    return render_template_string(HTML_TEMPLATE, user=None, verifying=True)

@app.route('/resend_code')
def resend_code():
    if 'verification_user_id' not in session:
        flash('Сначала зарегистрируйтесь', 'warning')
        return redirect(url_for('register'))
    
    user = User.query.get(session['verification_user_id'])
    if not user:
        session.pop('verification_user_id', None)
        flash('Ошибка', 'danger')
        return redirect(url_for('register'))
    
    new_code = user.generate_verification_code()
    db.session.commit()
    
    if send_verification_email(user.email, new_code):
        flash('Новый код отправлен на ваш email', 'success')
    else:
        flash('Ошибка при отправке кода', 'danger')
    
    return redirect(url_for('verify'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        login_input = request.form['login'].strip().lower()
        password = request.form['password']
        
        user = User.query.filter(
            (User.efm_id == login_input) | (User.email == login_input)
        ).first()
        
        if user and user.check_password(password):
            if user.is_banned:
                flash('Ваш аккаунт забанен', 'danger')
                return redirect(url_for('login'))
            
            if not user.is_verified:
                session['verification_user_id'] = user.id
                flash('Пожалуйста, подтвердите email', 'warning')
                return redirect(url_for('verify'))
            
            session['user_id'] = user.id
            flash(f'Добро пожаловать, {user.display_name}!', 'success')
            return redirect(url_for('index'))
        else:
            flash('Неверный логин (EFM ID/Email) или пароль', 'danger')
    
    return render_template_string(HTML_TEMPLATE, user=None)

@app.route('/logout')
def logout():
    session.clear()
    flash('Вы вышли из системы', 'info')
    return redirect(url_for('index'))

@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    user = get_current_user()
    
    if request.method == 'POST':
        action = request.form.get('action')
        
        if action == 'update_profile':
            new_display_name = request.form['display_name'].strip()
            if new_display_name:
                user.display_name = new_display_name
                flash('Имя обновлено', 'success')
        
        elif action == 'update_avatar':
            if 'avatar' in request.files:
                file = request.files['avatar']
                if file and allowed_file(file.filename):
                    # Обрабатываем и изменяем размер аватара
                    avatar_url = process_and_save_image(file, app.config['AVATAR_SIZE'], app.config['IMAGE_QUALITY'])
                    if avatar_url:
                        user.avatar_url = avatar_url
                        flash('Аватар обновлен', 'success')
                    else:
                        flash('Ошибка при обработке изображения', 'danger')
                else:
                    flash('Неподдерживаемый формат файла. Используйте PNG, JPG, GIF или WEBP', 'danger')
            
            elif 'avatar_base64' in request.form and request.form['avatar_base64']:
                avatar_url = save_base64_image(request.form['avatar_base64'], 'avatar')
                if avatar_url:
                    user.avatar_url = avatar_url
                    flash('Аватар обновлен', 'success')
                else:
                    flash('Ошибка при обработке изображения', 'danger')
        
        elif action == 'change_password':
            current_password = request.form['current_password']
            new_password = request.form['new_password']
            confirm_password = request.form['confirm_password']
            
            if not user.check_password(current_password):
                flash('Неверный текущий пароль', 'danger')
            elif new_password != confirm_password:
                flash('Новые пароли не совпадают', 'danger')
            elif len(new_password) < 6:
                flash('Пароль должен быть минимум 6 символов', 'danger')
            else:
                user.set_password(new_password)
                flash('Пароль успешно изменен', 'success')
        
        db.session.commit()
        return redirect(url_for('settings'))
    
    return render_template_string(HTML_TEMPLATE, user=user, settings_page=True)

@app.route('/delete_account', methods=['POST'])
@login_required
def delete_account():
    user = get_current_user()
    
    efm_id = request.form['efm_id'].strip()
    password = request.form['password']
    
    if user.efm_id != efm_id:
        flash('Неверный EFM ID', 'danger')
        return redirect(url_for('settings'))
    
    if not user.check_password(password):
        flash('Неверный пароль', 'danger')
        return redirect(url_for('settings'))
    
    user_email = user.email
    user_efm = user.efm_id
    
    db.session.delete(user)
    db.session.commit()
    
    send_account_deletion_email(user_email, user_efm)
    
    session.clear()
    
    flash('Аккаунт успешно удален', 'info')
    return redirect(url_for('index'))

@app.route('/post/create', methods=['POST'])
@login_required
def create_post():
    user = get_current_user()
    content = request.form['content'].strip()
    is_echo = request.form.get('is_echo') == 'on'
    
    if not content:
        flash('Пост не может быть пустым', 'danger')
        return redirect(url_for('index'))
    
    # Обрабатываем контент и ищем тег <cat>
    processed_content, cat_url = process_post_content(content)
    
    post = Post(
        content=processed_content,
        user_id=user.id,
        is_echo=is_echo
    )
    
    # Если есть тег <cat>, добавляем URL кота
    if cat_url:
        post.media_url = cat_url
    
    # Если есть загруженное изображение
    if 'media' in request.files:
        file = request.files['media']
        if file and allowed_file(file.filename):
            media_url = process_and_save_image(file, app.config['THUMBNAIL_SIZE'], app.config['IMAGE_QUALITY'])
            if media_url:
                post.media_url = media_url
    
    if is_echo:
        post.echo_expires_at = datetime.utcnow() + timedelta(hours=24)
    
    db.session.add(post)
    db.session.commit()
    
    flash('Пост опубликован!', 'success')
    return redirect(url_for('index'))

@app.route('/post/<int:post_id>/like', methods=['POST'])
@login_required
def like_post(post_id):
    user = get_current_user()
    post = Post.query.get_or_404(post_id)
    
    if post.author.is_banned:
        flash('Нельзя взаимодействовать с постами забаненных пользователей', 'danger')
        return redirect(url_for('index'))
    
    existing_like = Like.query.filter_by(user_id=user.id, post_id=post_id).first()
    
    if existing_like:
        db.session.delete(existing_like)
        post.likes_count -= 1
        flash('Лайк убран', 'info')
    else:
        like = Like(user_id=user.id, post_id=post_id)
        db.session.add(like)
        post.likes_count += 1
        flash('Пост понравился!', 'success')
    
    db.session.commit()
    return redirect(url_for('index'))

@app.route('/post/<int:post_id>/comment', methods=['POST'])
@login_required
def add_comment(post_id):
    user = get_current_user()
    post = Post.query.get_or_404(post_id)
    content = request.form['content'].strip()
    parent_id = request.form.get('parent_id')
    
    if not content:
        flash('Комментарий не может быть пустым', 'danger')
        return redirect(url_for('index'))
    
    if post.author.is_banned:
        flash('Нельзя комментировать посты забаненных пользователей', 'danger')
        return redirect(url_for('index'))
    
    comment = Comment(
        content=content,
        user_id=user.id,
        post_id=post_id,
        parent_id=parent_id if parent_id else None
    )
    
    db.session.add(comment)
    post.comments_count += 1
    db.session.commit()
    
    flash('Комментарий добавлен', 'success')
    return redirect(url_for('index'))

@app.route('/admin/users')
@admin_required
def admin_users():
    users = User.query.all()
    ban_logs = BanLog.query.order_by(BanLog.banned_at.desc()).limit(50).all()
    return render_template_string(HTML_TEMPLATE, 
                                 user=get_current_user(), 
                                 admin_users=users, 
                                 ban_logs=ban_logs,
                                 admin_page=True)

@app.route('/admin/ban/<int:user_id>', methods=['POST'])
@admin_required
def ban_user(user_id):
    admin = get_current_user()
    user_to_ban = User.query.get_or_404(user_id)
    reason = request.form['reason'].strip()
    
    if not reason:
        flash('Укажите причину бана', 'danger')
        return redirect(url_for('admin_users'))
    
    if user_to_ban.is_admin:
        flash('Нельзя забанить администратора', 'danger')
        return redirect(url_for('admin_users'))
    
    if user_to_ban.is_banned:
        flash('Пользователь уже забанен', 'warning')
        return redirect(url_for('admin_users'))
    
    user_to_ban.is_banned = True
    user_to_ban.banned_by = admin.id
    user_to_ban.banned_at = datetime.utcnow()
    
    ban_log = BanLog(
        admin_id=admin.id,
        banned_user_id=user_to_ban.id,
        reason=reason
    )
    db.session.add(ban_log)
    
    db.session.commit()
    
    flash(f'Пользователь {user_to_ban.efm_id} забанен. Все его посты удалены.', 'success')
    return redirect(url_for('admin_users'))

@app.route('/admin/unban/<int:user_id>', methods=['POST'])
@admin_required
def unban_user(user_id):
    user_to_unban = User.query.get_or_404(user_id)
    
    if not user_to_unban.is_banned:
        flash('Пользователь не забанен', 'warning')
        return redirect(url_for('admin_users'))
    
    user_to_unban.is_banned = False
    user_to_unban.banned_by = None
    user_to_unban.banned_at = None
    
    db.session.commit()
    
    flash(f'Пользователь {user_to_unban.efm_id} разбанен', 'success')
    return redirect(url_for('admin_users'))

@app.route('/admin/delete_post/<int:post_id>', methods=['POST'])
@admin_required
def admin_delete_post(post_id):
    post = Post.query.get_or_404(post_id)
    
    db.session.delete(post)
    db.session.commit()
    
    flash('Пост удален администратором', 'success')
    return redirect(url_for('index'))

# ==================== HTML ШАБЛОН ====================

HTML_TEMPLATE = '''
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>EFM ID - Социальная сеть</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            color: #333;
        }
        .container {
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
        }
        .header {
            background: white;
            border-radius: 15px;
            padding: 20px;
            margin-bottom: 30px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.1);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .logo {
            font-size: 28px;
            font-weight: bold;
            background: linear-gradient(135deg, #667eea, #764ba2);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        .user-info {
            display: flex;
            align-items: center;
            gap: 15px;
        }
        .avatar {
            width: 40px;
            height: 40px;
            border-radius: 50%;
            object-fit: cover;
            border: 2px solid #667eea;
        }
        .nav-links a {
            margin-left: 20px;
            text-decoration: none;
            color: #667eea;
            font-weight: 500;
        }
        .nav-links a:hover {
            color: #764ba2;
        }
        .content {
            background: white;
            border-radius: 15px;
            padding: 30px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.1);
        }
        .form-group {
            margin-bottom: 20px;
        }
        .form-group label {
            display: block;
            margin-bottom: 8px;
            font-weight: 500;
            color: #555;
        }
        .form-group input, .form-group textarea, .form-group select {
            width: 100%;
            padding: 12px;
            border: 2px solid #e0e0e0;
            border-radius: 8px;
            font-size: 16px;
            transition: border-color 0.3s;
        }
        .form-group input:focus, .form-group textarea:focus, .form-group select:focus {
            outline: none;
            border-color: #667eea;
        }
        .btn {
            padding: 12px 30px;
            border: none;
            border-radius: 8px;
            font-size: 16px;
            font-weight: 600;
            cursor: pointer;
            transition: transform 0.2s, box-shadow 0.2s;
            margin-right: 10px;
        }
        .btn-primary {
            background: linear-gradient(135deg, #667eea, #764ba2);
            color: white;
        }
        .btn-danger {
            background: linear-gradient(135deg, #f56565, #c53030);
            color: white;
        }
        .btn-warning {
            background: linear-gradient(135deg, #fbbf24, #d97706);
            color: white;
        }
        .btn-success {
            background: linear-gradient(135deg, #48bb78, #2f855a);
            color: white;
        }
        .btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 5px 15px rgba(0,0,0,0.2);
        }
        .alert {
            padding: 15px;
            border-radius: 8px;
            margin-bottom: 20px;
        }
        .alert-success {
            background: #c6f6d5;
            color: #22543d;
        }
        .alert-danger {
            background: #fed7d7;
            color: #742a2a;
        }
        .alert-info {
            background: #bee3f8;
            color: #2c5282;
        }
        .alert-warning {
            background: #feebc8;
            color: #744210;
        }
        .post {
            border: 1px solid #e0e0e0;
            border-radius: 10px;
            padding: 20px;
            margin-bottom: 20px;
            transition: box-shadow 0.3s;
        }
        .post:hover {
            box-shadow: 0 5px 20px rgba(0,0,0,0.1);
        }
        .post-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 15px;
        }
        .post-author {
            display: flex;
            align-items: center;
            gap: 10px;
            font-weight: bold;
            color: #667eea;
        }
        .post-author .avatar-small {
            width: 30px;
            height: 30px;
            border-radius: 50%;
            object-fit: cover;
        }
        .post-date {
            color: #999;
            font-size: 14px;
        }
        .post-content {
            margin-bottom: 15px;
            line-height: 1.6;
        }
        .post-media {
            margin: 15px 0;
            max-width: 100%;
            border-radius: 8px;
            overflow: hidden;
        }
        .post-media img {
            max-width: 100%;
            max-height: 400px;
            object-fit: contain;
            border-radius: 8px;
        }
        .post-stats {
            display: flex;
            gap: 20px;
            margin-bottom: 15px;
            color: #666;
        }
        .post-actions {
            display: flex;
            gap: 10px;
        }
        .echo-badge {
            display: inline-block;
            padding: 3px 8px;
            border-radius: 12px;
            font-size: 12px;
            font-weight: bold;
            margin-left: 10px;
        }
        .echo-active {
            background: #fbbf24;
            color: #744210;
        }
        .echo-survived {
            background: #48bb78;
            color: #22543d;
        }
        .comments-section {
            margin-top: 15px;
            padding-left: 20px;
            border-left: 3px solid #e0e0e0;
        }
        .comment {
            margin-bottom: 10px;
        }
        .comment-author {
            display: flex;
            align-items: center;
            gap: 8px;
            font-weight: bold;
            color: #667eea;
        }
        .comment-author .avatar-tiny {
            width: 20px;
            height: 20px;
            border-radius: 50%;
            object-fit: cover;
        }
        .comment-content {
            margin-top: 5px;
        }
        .reply-form {
            margin-top: 10px;
            margin-left: 20px;
        }
        .admin-section {
            margin-top: 20px;
            padding: 20px;
            background: #f0f0f0;
            border-radius: 8px;
        }
        .admin-badge {
            background: linear-gradient(135deg, #f56565, #c53030);
            color: white;
            padding: 2px 8px;
            border-radius: 12px;
            font-size: 12px;
            margin-left: 10px;
        }
        .banned-badge {
            background: #f56565;
            color: white;
            padding: 2px 8px;
            border-radius: 12px;
            font-size: 12px;
            margin-left: 10px;
        }
        .unverified-badge {
            background: #fbbf24;
            color: #744210;
            padding: 2px 8px;
            border-radius: 12px;
            font-size: 12px;
            margin-left: 10px;
        }
        table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 20px;
        }
        th, td {
            padding: 12px;
            text-align: left;
            border-bottom: 1px solid #e0e0e0;
        }
        th {
            background: #f7f7f7;
            font-weight: 600;
        }
        tr:hover {
            background: #f9f9f9;
        }
        .settings-section {
            margin-bottom: 30px;
            padding: 20px;
            border: 1px solid #e0e0e0;
            border-radius: 8px;
        }
        .danger-zone {
            border: 2px solid #f56565;
            background: #fff5f5;
        }
        .danger-zone h3 {
            color: #c53030;
        }
        .hint {
            color: #666;
            font-size: 14px;
            margin-top: 5px;
        }
        .current-avatar {
            display: flex;
            align-items: center;
            gap: 20px;
            margin-bottom: 20px;
        }
        .current-avatar img {
            width: 100px;
            height: 100px;
            border-radius: 50%;
            object-fit: cover;
            border: 3px solid #667eea;
        }
        .cat-tag {
            background: linear-gradient(135deg, #fbbf24, #d97706);
            color: white;
            padding: 2px 8px;
            border-radius: 12px;
            font-size: 12px;
            display: inline-block;
            margin-bottom: 10px;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div class="logo">EFM ID</div>
            <div class="nav-links">
                {% if user %}
                    <div class="user-info">
                        <img src="{{ user.avatar_url }}" alt="Avatar" class="avatar">
                        <span>{{ user.display_name }}</span>
                    </div>
                    <a href="{{ url_for('index') }}">Главная</a>
                    <a href="{{ url_for('settings') }}">Настройки</a>
                    {% if user.is_admin %}
                        <a href="{{ url_for('admin_users') }}">Админ-панель</a>
                    {% endif %}
                    <a href="{{ url_for('logout') }}">Выйти</a>
                {% else %}
                    <a href="{{ url_for('index') }}">Главная</a>
                    <a href="{{ url_for('login') }}">Вход</a>
                    <a href="{{ url_for('register') }}">Регистрация</a>
                {% endif %}
            </div>
        </div>
        
        <div class="content">
            {% with messages = get_flashed_messages(with_categories=true) %}
                {% if messages %}
                    {% for category, message in messages %}
                        <div class="alert alert-{{ category }}">{{ message }}</div>
                    {% endfor %}
                {% endif %}
            {% endwith %}
            
            {% if request.path == '/register' and not user %}
                <h2>Регистрация в EFM ID</h2>
                <form method="POST">
                    <div class="form-group">
                        <label>EFM ID (уникальное имя)</label>
                        <input type="text" name="efm_id" required placeholder="например: john123">
                        <div class="hint">Только буквы и цифры, без @. Имя 'admin' занято</div>
                    </div>
                    <div class="form-group">
                        <label>Email</label>
                        <input type="email" name="email" required placeholder="your@email.com">
                    </div>
                    <div class="form-group">
                        <label>Отображаемое имя</label>
                        <input type="text" name="display_name" required placeholder="Как вас называть">
                    </div>
                    <div class="form-group">
                        <label>Пароль (минимум 6 символов)</label>
                        <input type="password" name="password" required>
                    </div>
                    <button type="submit" class="btn btn-primary">Зарегистрироваться</button>
                </form>
                <p style="margin-top: 20px">Уже есть аккаунт? <a href="{{ url_for('login') }}">Войти</a></p>
            
            {% elif verifying %}
                <h2>Подтверждение email</h2>
                <p>На ваш email отправлен код подтверждения. Введите его ниже:</p>
                <form method="POST">
                    <div class="form-group">
                        <label>Код подтверждения</label>
                        <input type="text" name="code" required maxlength="6" pattern="[0-9]{6}" placeholder="6 цифр">
                    </div>
                    <button type="submit" class="btn btn-primary">Подтвердить</button>
                    <a href="{{ url_for('resend_code') }}" class="btn btn-warning">Отправить код повторно</a>
                </form>
            
            {% elif request.path == '/login' and not user %}
                <h2>Вход в EFM ID</h2>
                <form method="POST">
                    <div class="form-group">
                        <label>EFM ID или Email</label>
                        <input type="text" name="login" required placeholder="например: john123 или email@mail.ru">
                        <div class="hint">Можно ввести EFM ID (без @) или email</div>
                    </div>
                    <div class="form-group">
                        <label>Пароль</label>
                        <input type="password" name="password" required>
                    </div>
                    <button type="submit" class="btn btn-primary">Войти</button>
                </form>
                <p style="margin-top: 20px">Нет аккаунта? <a href="{{ url_for('register') }}">Зарегистрироваться</a></p>
            
            {% elif settings_page %}
                <h2>Настройки профиля</h2>
                
                <div class="settings-section">
                    <h3>Аватар</h3>
                    <div class="current-avatar">
                        <img src="{{ user.avatar_url }}" alt="Current avatar">
                        <div>
                            <p>Текущий аватар</p>
                        </div>
                    </div>
                    <form method="POST" enctype="multipart/form-data">
                        <input type="hidden" name="action" value="update_avatar">
                        <div class="form-group">
                            <label>Загрузить новый аватар</label>
                            <input type="file" name="avatar" accept="image/*">
                            <div class="hint">Рекомендуемый размер: 200x200 пикселей. Поддерживаются: PNG, JPG, GIF, WEBP</div>
                        </div>
                        <button type="submit" class="btn btn-primary">Обновить аватар</button>
                    </form>
                </div>
                
                <div class="settings-section">
                    <h3>Основные настройки</h3>
                    <form method="POST">
                        <input type="hidden" name="action" value="update_profile">
                        <div class="form-group">
                            <label>EFM ID (нельзя изменить)</label>
                            <input type="text" value="{{ user.efm_id }}" disabled>
                        </div>
                        <div class="form-group">
                            <label>Email (скрыт от других)</label>
                            <input type="email" value="{{ user.email }}" disabled>
                            <div class="hint">Email нельзя изменить</div>
                        </div>
                        <div class="form-group">
                            <label>Отображаемое имя</label>
                            <input type="text" name="display_name" value="{{ user.display_name }}" required>
                        </div>
                        <button type="submit" class="btn btn-primary">Сохранить имя</button>
                    </form>
                </div>
                
                <div class="settings-section">
                    <h3>Смена пароля</h3>
                    <form method="POST">
                        <input type="hidden" name="action" value="change_password">
                        <div class="form-group">
                            <label>Текущий пароль</label>
                            <input type="password" name="current_password" required>
                        </div>
                        <div class="form-group">
                            <label>Новый пароль (минимум 6 символов)</label>
                            <input type="password" name="new_password" required>
                        </div>
                        <div class="form-group">
                            <label>Подтвердите новый пароль</label>
                            <input type="password" name="confirm_password" required>
                        </div>
                        <button type="submit" class="btn btn-primary">Сменить пароль</button>
                    </form>
                </div>
                
                <div class="settings-section danger-zone">
                    <h3>Опасная зона</h3>
                    <p>Удаление аккаунта приведет к безвозвратному удалению всех ваших постов, комментариев и лайков.</p>
                    <form method="POST" action="{{ url_for('delete_account') }}" onsubmit="return confirm('Вы уверены, что хотите удалить аккаунт? Это действие необратимо!');">
                        <div class="form-group">
                            <label>Введите ваш EFM ID для подтверждения</label>
                            <input type="text" name="efm_id" required placeholder="Например: john123">
                        </div>
                        <div class="form-group">
                            <label>Введите ваш пароль</label>
                            <input type="password" name="password" required>
                        </div>
                        <button type="submit" class="btn btn-danger">Удалить аккаунт</button>
                    </form>
                </div>
            
            {% elif admin_page %}
                <h2>Админ-панель</h2>
                
                <h3>Пользователи</h3>
                <table>
                    <tr>
                        <th>Аватар</th>
                        <th>ID</th>
                        <th>EFM ID</th>
                        <th>Email</th>
                        <th>Имя</th>
                        <th>Статус</th>
                        <th>Дата регистрации</th>
                        <th>Действия</th>
                    </tr>
                    {% for u in admin_users %}
                    <tr>
                        <td><img src="{{ u.avatar_url }}" alt="Avatar" style="width: 30px; height: 30px; border-radius: 50%; object-fit: cover;"></td>
                        <td>{{ u.id }}</td>
                        <td>{{ u.efm_id }}</td>
                        <td>{{ u.email }}</td>
                        <td>{{ u.display_name }}</td>
                        <td>
                            {% if u.is_admin %}
                                <span class="admin-badge">Админ</span>
                            {% endif %}
                            {% if u.is_banned %}
                                <span class="banned-badge">Забанен</span>
                            {% endif %}
                            {% if not u.is_verified %}
                                <span class="unverified-badge">Не подтвержден</span>
                            {% endif %}
                        </td>
                        <td>{{ u.created_at.strftime('%Y-%m-%d %H:%M') }}</td>
                        <td>
                            {% if not u.is_admin and u.id != user.id %}
                                {% if not u.is_banned %}
                                    <form method="POST" action="{{ url_for('ban_user', user_id=u.id) }}" style="display: inline;">
                                        <input type="text" name="reason" placeholder="Причина бана" required>
                                        <button type="submit" class="btn btn-danger">Забанить</button>
                                    </form>
                                {% else %}
                                    <form method="POST" action="{{ url_for('unban_user', user_id=u.id) }}" style="display: inline;">
                                        <button type="submit" class="btn btn-success">Разбанить</button>
                                    </form>
                                {% endif %}
                            {% endif %}
                        </td>
                    </tr>
                    {% endfor %}
                </table>
                
                <h3>Последние баны</h3>
                <table>
                    <tr>
                        <th>Админ</th>
                        <th>Забанен</th>
                        <th>Причина</th>
                        <th>Дата</th>
                    </tr>
                    {% for log in ban_logs %}
                    <tr>
                        <td>{{ log.admin.efm_id }}</td>
                        <td>{{ log.banned_user.efm_id }}</td>
                        <td>{{ log.reason }}</td>
                        <td>{{ log.banned_at.strftime('%Y-%m-%d %H:%M') }}</td>
                    </tr>
                    {% endfor %}
                </table>
            
            {% else %}
                {% if user %}
                    <h2>Добро пожаловать, {{ user.display_name }}!</h2>
                    
                    <form method="POST" action="{{ url_for('create_post') }}" enctype="multipart/form-data" style="margin-bottom: 30px;">
                        <div class="form-group">
                            <textarea name="content" rows="3" placeholder="Что у вас нового? Используйте <cat> для случайного кота" required></textarea>
                        </div>
                        <div class="form-group">
                            <label>Изображение (необязательно)</label>
                            <input type="file" name="media" accept="image/*">
                            <div class="hint">Максимальный размер: 800x800 пикселей</div>
                        </div>
                        <div class="form-group">
                            <label>
                                <input type="checkbox" name="is_echo"> Эхо-пост (исчезнет через 24 часа, если не наберет 100 лайков)
                            </label>
                        </div>
                        <button type="submit" class="btn btn-primary">Опубликовать</button>
                    </form>
                {% else %}
                    <h2>Добро пожаловать в EFM ID!</h2>
                    <p>Социальная сеть с уникальными идентификаторами и эхо-постами. <a href="{{ url_for('register') }}">Зарегистрируйтесь</a> или <a href="{{ url_for('login') }}">войдите</a>, чтобы начать.</p>
                {% endif %}
                
                <h3>Лента постов</h3>
                {% for post in posts %}
                    <div class="post">
                        <div class="post-header">
                            <div class="post-author">
                                <img src="{{ post.author.avatar_url }}" alt="Avatar" class="avatar-small">
                                <span>{{ post.author.display_name }} (@{{ post.author.efm_id }})</span>
                            </div>
                            <span class="post-date">{{ post.created_at.strftime('%Y-%m-%d %H:%M') }}</span>
                        </div>
                        
                        {% if post.is_echo %}
                            <span class="echo-badge echo-active">Эхо (до {{ post.echo_expires_at.strftime('%H:%M %d.%m') }})</span>
                        {% elif post.echo_survived %}
                            <span class="echo-badge echo-survived">Выжившее эхо</span>
                        {% endif %}
                        
                        {% if '<cat>' in post.content %}
                            <span class="cat-tag">🐱 Случайный кот</span>
                        {% endif %}
                        
                        <div class="post-content">
                            {{ post.content }}
                        </div>
                        
                        {% if post.media_url %}
                            <div class="post-media">
                                <img src="{{ post.media_url }}" alt="Post media" loading="lazy">
                            </div>
                        {% endif %}
                        
                        <div class="post-stats">
                            <span>❤️ {{ post.likes_count }} лайков</span>
                            <span>💬 {{ post.comments_count }} комментариев</span>
                        </div>
                        
                        <div class="post-actions">
                            {% if user %}
                                <form method="POST" action="{{ url_for('like_post', post_id=post.id) }}" style="display: inline;">
                                    <button type="submit" class="btn btn-primary">❤️ Лайк</button>
                                </form>
                                
                                {% if user.is_admin %}
                                    <form method="POST" action="{{ url_for('admin_delete_post', post_id=post.id) }}" style="display: inline;" onsubmit="return confirm('Удалить этот пост?');">
                                        <button type="submit" class="btn btn-danger">Удалить (админ)</button>
                                    </form>
                                {% endif %}
                            {% endif %}
                        </div>
                        
                        <div class="comments-section">
                            <h4>Комментарии</h4>
                            
                            {% for comment in post.comments if not comment.parent_id %}
                                <div class="comment">
                                    <div class="comment-author">
                                        <img src="{{ comment.author.avatar_url }}" alt="Avatar" class="avatar-tiny">
                                        <span>{{ comment.author.display_name }}:</span>
                                    </div>
                                    <div class="comment-content">{{ comment.content }}</div>
                                    <small>{{ comment.created_at.strftime('%H:%M %d.%m') }}</small>
                                    
                                    {% if user %}
                                        <form method="POST" action="{{ url_for('add_comment', post_id=post.id) }}" class="reply-form">
                                            <input type="hidden" name="parent_id" value="{{ comment.id }}">
                                            <input type="text" name="content" placeholder="Ответить..." required>
                                            <button type="submit" class="btn btn-primary">Ответить</button>
                                        </form>
                                    {% endif %}
                                    
                                    {% for reply in comment.replies %}
                                        <div class="comment" style="margin-left: 20px;">
                                            <div class="comment-author">
                                                <img src="{{ reply.author.avatar_url }}" alt="Avatar" class="avatar-tiny">
                                                <span>{{ reply.author.display_name }}:</span>
                                            </div>
                                            <div class="comment-content">{{ reply.content }}</div>
                                            <small>{{ reply.created_at.strftime('%H:%M %d.%m') }}</small>
                                        </div>
                                    {% endfor %}
                                </div>
                            {% endfor %}
                            
                            {% if user %}
                                <form method="POST" action="{{ url_for('add_comment', post_id=post.id) }}" style="margin-top: 15px;">
                                    <input type="text" name="content" placeholder="Написать комментарий..." required>
                                    <button type="submit" class="btn btn-primary">Отправить</button>
                                </form>
                            {% endif %}
                        </div>
                    </div>
                {% else %}
                    <p>Пока нет постов. Будьте первым!</p>
                {% endfor %}
            {% endif %}
        </div>
    </div>
</body>
</html>
'''
# ==================== ИНИЦИАЛИЗАЦИЯ БД ====================

def init_db():
    """Принудительно создает все таблицы при запуске"""
    with app.app_context():
        print("🔄 Создание таблиц базы данных...")
        db.create_all()
        create_admin()
        print("✅ База данных готова")

# Запускаем инициализацию
init_db()
# ==================== ЗАПУСК ====================

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        create_admin()
    app.run(debug=True, host='0.0.0.0', port=5000)
