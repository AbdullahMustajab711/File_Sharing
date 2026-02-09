import os
import datetime
from flask import Flask, render_template, request, jsonify, send_from_directory
from flask_pymongo import PyMongo
from flask_bcrypt import Bcrypt
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.utils import secure_filename
from bson.objectid import ObjectId 

app = Flask(__name__)

# --- CONFIGURATION ---
app.config['SECRET_KEY'] = 'super-secret-key-change-me-in-production' 
app.config['MONGO_URI'] = 'mongodb://localhost:27017/cloudshare_db'
app.config['UPLOAD_FOLDER'] = 'uploads'

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# --- INITIALIZATIONS ---
mongo = PyMongo(app)
bcrypt = Bcrypt(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login' 

# --- DATABASE MODELS ---
class User(UserMixin):
    def __init__(self, user_data):
        self.id = str(user_data['_id'])
        self.name = user_data['name']
        self.email = user_data['email']

@login_manager.user_loader
def load_user(user_id):
    try:
        user_data = mongo.db.users.find_one({"_id": ObjectId(user_id)})
        if user_data:
            return User(user_data)
    except Exception:
        return None
    return None

# --- ROUTES ---

@app.route('/')
def index():
    return render_template('index.html')

# --- AUTH (Login Only) ---
@app.route('/api/auth/login', methods=['POST'])
def login():
    data = request.json
    user = mongo.db.users.find_one({"email": data.get('email')})
    if user and bcrypt.check_password_hash(user['password'], data.get('password')):
        login_user(User(user))
        return jsonify({"message": "Welcome back", "user": {"name": user['name'], "email": user['email']}}), 200
    return jsonify({"error": "Invalid email or password"}), 401

@app.route('/api/auth/logout', methods=['POST'])
@login_required
def logout():
    logout_user()
    return jsonify({"message": "Logged out"}), 200

# --- FILES ---
@app.route('/api/files/upload', methods=['POST'])
@login_required
def upload_file():
    if 'file' not in request.files: return jsonify({"error": "No file"}), 400
    file = request.files['file']
    if file.filename == '': return jsonify({"error": "No file selected"}), 400
    
    filename = secure_filename(file.filename)
    unique_filename = f"{current_user.id}_{filename}"
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], unique_filename)
    file.save(file_path)

    mongo.db.files.insert_one({
        "user_id": current_user.id, "original_name": filename, "stored_name": unique_filename,
        "file_type": file.content_type, "size": os.path.getsize(file_path),
        "folder_id": request.form.get('folder_id') or None, "is_favorite": False, "is_trashed": False,
        "uploaded_at": datetime.datetime.utcnow()
    })
    return jsonify({"message": "Uploaded"}), 201

@app.route('/api/files', methods=['GET'])
@login_required
def get_files():
    view_type = request.args.get('view', 'all')
    folder_id = request.args.get('folder_id')
    sort_by = request.args.get('sort', 'uploaded_at')
    search_q = request.args.get('search')

    query = {"user_id": current_user.id}

    if search_q:
        query["original_name"] = {"$regex": search_q, "$options": "i"}
    elif view_type == 'favorites':
        query["is_favorite"] = True; query["is_trashed"] = False
    elif view_type == 'trash':
        query["is_trashed"] = True
    elif folder_id:
        query["folder_id"] = folder_id; query["is_trashed"] = False
    else:
        query["folder_id"] = None; query["is_trashed"] = False

    sort_field = 'original_name' if sort_by == 'name' else ('size' if sort_by == 'size' else 'uploaded_at')
    files = list(mongo.db.files.find(query).sort(sort_field, -1))
    
    for f in files:
        f['_id'] = str(f['_id'])
        f['upload_date'] = f['uploaded_at'].strftime('%b %d, %Y')
    return jsonify(files), 200

@app.route('/download/<filename>')
@login_required
def download_file(filename):
    file_record = mongo.db.files.find_one({"stored_name": filename, "user_id": current_user.id})
    if not file_record: return "Unauthorized", 403
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename, as_attachment=True, download_name=file_record['original_name'])

@app.route('/api/files/<file_id>', methods=['PUT'])
@login_required
def update_file(file_id):
    data = request.json
    file_obj = mongo.db.files.find_one({"_id": ObjectId(file_id), "user_id": current_user.id})
    if not file_obj: return jsonify({"error": "File not found"}), 404

    update_ops = {}
    if 'new_name' in data: update_ops["original_name"] = data['new_name']
    if 'is_favorite' in data: update_ops["is_favorite"] = data['is_favorite']
    if 'is_trashed' in data: update_ops["is_trashed"] = data['is_trashed']
    if 'new_folder_id' in data:
        target_id = data['new_folder_id']
        if target_id:
            if not mongo.db.folders.find_one({"_id": ObjectId(target_id), "user_id": current_user.id}):
                return jsonify({"error": "Invalid folder"}), 400
            update_ops["folder_id"] = target_id
        else:
            update_ops["folder_id"] = None

    mongo.db.files.update_one({"_id": ObjectId(file_id)}, {"$set": update_ops})
    return jsonify({"message": "Updated"}), 200

@app.route('/api/files/<file_id>', methods=['DELETE'])
@login_required
def delete_file(file_id):
    file_obj = mongo.db.files.find_one({"_id": ObjectId(file_id), "user_id": current_user.id})
    if file_obj:
        mongo.db.files.delete_one({"_id": ObjectId(file_id)})
        try: os.remove(os.path.join(app.config['UPLOAD_FOLDER'], file_obj['stored_name']))
        except: pass
        return jsonify({"message": "Deleted"}), 200
    return jsonify({"error": "Not found"}), 404

# --- FOLDERS ---
@app.route('/api/folders', methods=['POST'])
@login_required
def create_folder():
    data = request.json
    result = mongo.db.folders.insert_one({
        "name": data['name'], "user_id": current_user.id, "created_at": datetime.datetime.utcnow()
    })
    return jsonify({"_id": str(result.inserted_id), "name": data['name']}), 201

@app.route('/api/folders', methods=['GET'])
@login_required
def get_folders():
    folders = list(mongo.db.folders.find({"user_id": current_user.id}))
    for f in folders: f['_id'] = str(f['_id'])
    return jsonify(folders), 200

@app.route('/api/folders/<folder_id>', methods=['PUT'])
@login_required
def update_folder(folder_id):
    data = request.json
    if not mongo.db.folders.find_one({"_id": ObjectId(folder_id), "user_id": current_user.id}):
        return jsonify({"error": "Not found"}), 404
    if 'new_name' in data:
        mongo.db.folders.update_one({"_id": ObjectId(folder_id)}, {"$set": {"name": data['new_name']}})
        return jsonify({"message": "Updated"}), 200
    return jsonify({"error": "Bad request"}), 400

@app.route('/api/folders/<folder_id>', methods=['DELETE'])
@login_required
def delete_folder(folder_id):
    folder = mongo.db.folders.find_one({"_id": ObjectId(folder_id), "user_id": current_user.id})
    if not folder: return jsonify({"error": "Not found"}), 404
    mongo.db.files.update_many({"folder_id": folder_id}, {"$set": {"folder_id": None}})
    mongo.db.folders.delete_one({"_id": ObjectId(folder_id)})
    return jsonify({"message": "Deleted"}), 200

if __name__ == '__main__':
    app.run(debug=True)