from vinetrimmer.utils.MSL.MSLObject import MSLObject


class MSLKeys(MSLObject):
    def __init__(self, encryption=None, sign=None, rsa=None, mastertoken=None, cdm_session=None):
        self.encryption = encryption
        self.sign = sign
        self.rsa = rsa
        self.mastertoken = mastertoken
        self.cdm_session = cdm_session
