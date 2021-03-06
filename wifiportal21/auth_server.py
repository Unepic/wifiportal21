#!/usr/bin/env python

import logging

import flask
import requests
from flask import Flask
from flask import render_template
from flask_sqlalchemy import SQLAlchemy

import qrcode
import base64
import io

import uuid

# change the receiving_key in config.py in the root folder.
from config import receiving_key, SATOSHIS_PER_MINUTE, BitCoreURL
from pycoin.key.BIP32Node import BIP32Node
from sqlalchemy.sql.functions import func

# logging.basicConfig(level=logging.DEBUG)
logging.basicConfig(level=logging.ERROR)

auth_app = Flask(__name__, static_folder='static')
#auth_app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:////tmp/wifiportal21.db'
auth_app.config['SQLALCHEMY_DATABASE_URI'] = 'mysql+pymysql://wifiportal:wifiportalpw@localhost/wifiportal'

db = SQLAlchemy(auth_app)

receiving_account = BIP32Node.from_text(receiving_key)


SATOSHIS_PER_MBTC = 100 * 10 ** 3
SATOSHIS_PER_BTC = 100 * 10 ** 6

STATUS_NONE = 0
STATUS_PAYREQ = 1
STATUS_PAID = 2

RECEIVING = 0

class Guest(db.Model):
    uuid = db.Column(db.String(255), primary_key=True)
    mac = db.Column(db.String(17), unique=True)
    address = db.Column(db.String(40), unique=True)
    address_index = db.Column(db.Integer(), unique=True)
    status = db.Column(db.Integer())
    minutes = db.Column(db.Integer())
    last_balance = db.Column(db.Integer())  # Store the amount of sat in the account last time we verified so we don't repeatedly authorize

    def __init__(self, uuid, mac):
        self.uuid = uuid
        self.mac = mac
        self.address = None
        self.status = STATUS_NONE
        self.minutes = -1
        self.last_balance = 0

    def __repr__(self):
        return "UUID: {0}\nMAC: {1}\nStatus: {2}\nAddress: {3}\nMinutes: {4}\nLast Balance: {5}\nKey Index: {6}".format(self.uuid, self.mac, self.status, self.address, self.minutes, self.last_balance, self.address_index)

db.create_all()

@auth_app.route('/wifidog/login/', methods=[ 'GET', 'POST' ])
def client_login():
    gw_address = flask.request.args.get('gw_address')
    gw_port = flask.request.args.get('gw_port')
    success_URL = flask.request.args.get('url')
    token = uuid.uuid4()
    auth_URL = "http://{0}:{1}/wifidog/auth?token={2}".format(gw_address, gw_port, token)
    price = "The cost of this service is {0:1.6f} BTC, or {1:1.2f} mBTC or {2:,} satoshis per minute".format(SATOSHIS_PER_MINUTE / SATOSHIS_PER_BTC, SATOSHIS_PER_MINUTE / SATOSHIS_PER_MBTC, SATOSHIS_PER_MINUTE)
    portal_html = render_template('portal.html', auth_URL=auth_URL, token=token, price=price, success_URL=success_URL)
    return portal_html

@auth_app.route('/wifidog/auth/')
def client_auth():
    stage = flask.request.args.get('stage')
    mac = flask.request.args.get('mac')
    uuid = flask.request.args.get('token')
    guest = Guest.query.filter_by(mac=mac).first()

    if guest:  # Existing Guest
        if guest.uuid != uuid:  # Old UUID, update it
            # print("Found existing under different uuid {0}".format(guest.uuid))
            guest.uuid = uuid  # Update UUID in guest
            if guest.status == STATUS_PAID and guest.minutes <= 0:  # Old guest without balance
                guest.status = STATUS_PAYREQ
            db.session.commit()
    else:  # New Guest
        guest = Guest(uuid, mac)
        db.session.add(guest)
        db.session.commit()

    if stage == "login":
        if guest.status == STATUS_NONE:
            return ("Auth: -1" , 200)  # Auth - Invalid
        elif guest.status == STATUS_PAID:
            if guest.minutes > 0:
                return("Auth: 1", 200)  # Paid, give access!
            else:
                guest.status = STATUS_NONE
                return ("Auth: -1" , 200)  # Auth - Invalid
        elif guest.status == STATUS_PAYREQ:
                return ("Auth: -1" , 200)  # Auth - Invalid

    elif stage == "counters":
        guest = Guest.query.filter_by(uuid=uuid).first()
        if guest.minutes > 0:
            guest.minutes -= 1
            db.session.commit()
            print("Guest accounting, {0} minutes remain".format(guest.minutes))
            return("Auth: 1", 200)  # Paid, give access!
        else:
            # print("Guest {0} not yet paid".format(uuid))
            if guest.status == STATUS_PAID:  # No more minutes left, restart payment request
                guest.status = STATUS_PAYREQ
            return ("Auth: 0" , 200)  # Auth - Invalid
    else:
        raise Exception("Unknown authorization stage {0}".format(stage))


@auth_app.route('/auth_status')
def auth_status():
    uuid = flask.request.args.get('token')
    guest = Guest.query.filter_by(uuid=uuid).first()
    if not guest:
        # print("Unregistered guest {0}".format(uuid))
        return "Must register first", 404
    try:
        # print("Returning status {0} for {1}".format(guest.status, guest.uuid))
        status_response = { 'status' : guest.status }
        return flask.json.dumps(status_response)
    except:
        raise Exception("Error finding guest status {0}".format(uuid))


def inline_base64_qrcode(address):
    qr = qrcode.make("bitcoin:{0}".format(address), error_correction=qrcode.constants.ERROR_CORRECT_L)
    output = io.BytesIO()
    qr.save(output, 'PNG')
    output.seek(0)
    qr_base64 = base64.b64encode(output.read()).decode()
    return qr_base64

def get_unconfirmed_balance_blockchain(address):
    r = requests.get('https://blockchain.info/unspent?active={0}'.format(address))
    print("Checking balance for {0}".format(address))
    balance = 0
    if r.status_code == 200:
        utxo_response = r.json()
        if 'unspent_outputs' in utxo_response:
            for utxo in utxo_response['unspent_outputs']:
                if 'value' in utxo:
                    balance += utxo['value']
        print("Balance for {0} is {1}".format(address, balance))
        return balance
    elif r.status_code == 500:  # No UTXO to spend
        return balance
    else:
        raise Exception("Error checking balance, unexpected HTTP code: {0} {1}".format(r.status_code, r.text))

def get_unconfirmed_balance_bitcore(address):
    r = requests.get('https://{0}/addr/{1}/unconfirmedBalance'.format(BitCoreURL, address))
    print("Checking balance for {0}".format(address))
    balance = 0
    if r.status_code == 200:
        balance = int(r.text)
        print("Balance for {0} is {1}".format(address, balance))
        return balance
    elif r.status_code == 500:  # No UTXO to spend
        return balance
    else:
        raise Exception("Error checking balance, unexpected HTTP code: {0} {1}".format(r.status_code, r.text))

def get_unconfirmed_balance(address):
    return get_unconfirmed_balance_bitcore(address)
    # get_unconfirmed_balance_blockchain(address)

@auth_app.route('/static/<path:path>')
@auth_app.route('/js/<path:path>')
def static_jquery(path):
    return flask.send_from_directory(auth_app.static_folder, path)

@auth_app.route('/get_payment_address')
def get_payment_address():
    uuid = flask.request.args.get('token')
    guest = Guest.query.filter_by(uuid=uuid).first()
    # TODO: Add check on guest to make sure it's an object
    if guest.status == STATUS_NONE or guest.status == STATUS_PAYREQ:
        guest.status = STATUS_PAYREQ
        if not guest.address:
            guest.address = generate_new_address()

        db.session.commit()
        qr = inline_base64_qrcode(guest.address)
        response = {'address': guest.address, 'qr': qr}
        return flask.json.dumps(response), 200
    else:
        return('must register first', 404)

def generate_new_address(index=None):
    if index is None:
        result = db.session.query(func.max(Guest.address_index).label("max_index")).one()
        if result and result.max_index:
            index = result.max_index + 1
        else:
            index = 0
    return receiving_account.subkey(RECEIVING).subkey(index).address()

@auth_app.route('/check_payment')
def check_payment():
    uuid = flask.request.args.get('token')
    guest = Guest.query.filter_by(uuid=uuid).first()
    # assert guest
    # assert guest.status == STATUS_PAYREQ
    # assert guest.address
    address = guest.address
    unconf_balance = get_unconfirmed_balance(address)
    # We check the last balance vs the current balance to see if they have paid more
    # since the last time we authorized them
    if unconf_balance > 0 and unconf_balance > guest.last_balance:  # Payment detected on this address
        guest.status = STATUS_PAID
        minutes = unconf_balance // SATOSHIS_PER_MINUTE
        guest.last_balance = unconf_balance
        # assert minutes > 0
        # print("Allocating {0} satoshis, {1} minutes to guest {2}".format(unconf_balance,minutes, uuid))
        guest.minutes = minutes
        db.session.commit()
        return("Payment received", 200)
    else:
        return("Waiting for payment", 402)


@auth_app.route('/wifidog/ping/')
def gw_ping():
    # print(db.session.query(Guest).all())
    return ('Pong', 200)

def run_server(host='0.0.0.0', port=21142):
    auth_app.run(host=host, port=port)

if __name__ == '__main__':
    run_server()
