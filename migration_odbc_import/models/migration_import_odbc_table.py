# -*- coding: utf-8 -*-
import pyodbc
import requests
import base64
import logging
_logger = logging.getLogger(__name__)
from datetime import datetime
from openerp.tools import DEFAULT_SERVER_DATETIME_FORMAT

from openerp import api, tools, fields, models
from odoo.exceptions import ValidationError, UserError

class MigrationImportOdbcTable(models.Model):

    _name = "migration.import.odbc.table"

    import_id = fields.Many2one('migration.import.odbc', string="Import ID")
    name = fields.Char(string="Name", readonly="True")
    model_id = fields.Many2one('ir.model', string="Model", help="The ORM model which the data gets imported into")
    field_count = fields.Integer(string="Fields", compute="_compute_field_count")
    db_field_ids = fields.One2many('migration.import.odbc.table.field', 'table_id', string="Database Fields")
    default_value_ids = fields.One2many('migration.import.odbc.table.default', 'table_id', string="Default Values", help="Set a value before importing into the record into the database")
    select_sql = fields.Char(string="Select SQL", help="Modify this if you want to perform sql transformations such as concat first and last name into name field")
    relationship_ids = fields.One2many('migration.import.odbc.relationship', 'table1', string="Database Relatioships")
    file_download_ids = fields.One2many('migration.import.odbc.table.download', 'table_id', string="File Import")
    where_clause = fields.Char(string="SQL WHERE Clause", default="WHERE 1=1")

    @api.one
    @api.depends('db_field_ids')
    def _compute_field_count(self):
        self.field_count = len(self.db_field_ids)

    def get_table_relationships(self):
        """ Query the schema and build a relationship table """

        sql = "SELECT * FROM `INFORMATION_SCHEMA`.`KEY_COLUMN_USAGE` WHERE `TABLE_SCHEMA` = SCHEMA() AND `REFERENCED_TABLE_NAME` IS NOT NULL"
        conn = pyodbc.connect(self.import_id.connection_string)
        cursor = conn.cursor()
        cursor.execute(sql)

        columns = [column[0] for column in cursor.description]
        results = []
        for row in cursor.fetchall():
            
            #Merge the defaults with sql dictionary
            row_dict = dict(zip(columns, row))
        
            _logger.error(row_dict['TABLE_NAME'])
        
    def import_table_data_wrapper(self):
        #Create a import log just for this table
        import_log = self.env['migration.import.odbc.log'].create({'import_id': self.import_id.id, 'state':'progress'})
        self.import_table_data(import_log)
        import_log.state = "done"
        import_log.finish_date = datetime.utcnow()

    def import_files(self):
        conn = pyodbc.connect(self.import_id.connection_string)
        cursor = conn.cursor()
        cursor.execute(self.select_sql)

        columns = [column[0] for column in cursor.description]
        results = []
        for row in cursor.fetchall():
            
            #Merge the defaults with sql dictionary
            row_dict = dict(zip(columns, row))

            if 'id' in row_dict:
                external_identifier = "import_record_" + self.model_id.model.replace(".","_") + "_" + str(row_dict['id'])
                
                existing_record = self.env['ir.model.data'].xmlid_to_object('odbc_import.' + external_identifier)
                if existing_record:
                    write_dict = {}
                    for download_file in self.file_download_ids:
                        url = download_file.download_url
                        
                        for column in row_dict:
                            url = url.replace("${" + column + "}", str(row_dict[column]) )
                            

                        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_11_2) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/47.0.2526.106 Safari/537.36"}
                        r = requests.get(url, headers=headers)
                        image_data = base64.b64encode( r.content )
 
                        if r.status_code == 200:
                            write_dict[download_file.field_id.name] = image_data
                        
                    existing_record.write(write_dict)
        
    def import_table_data(self, import_log):
        conn = pyodbc.connect(self.import_id.connection_string)
        cursor = conn.cursor()
        cursor.execute(self.select_sql)

        import_log_table = self.env['migration.import.odbc.log.table'].create({'log_id':import_log.id, 'table_id': self.id, 'total_records': len(list(cursor)) })

        import_record_count = import_log_table.imported_records

        cursor.execute(self.select_sql) #Issue cursor is lost? after counting total record?!?
        
        defaults = {}
        for default in self.default_value_ids:
            defaults[default.field_id.name] = default.value

        columns = [column[0] for column in cursor.description]
        results = []
        for row in cursor.fetchall():
            
            #Merge the defaults with sql dictionary
            row_dict = dict(zip(columns, row))
            merged_dict = defaults.copy()
            merged_dict.update(row_dict)
            
            if 'id' in merged_dict:
                external_identifier = "import_record_" + self.model_id.model.replace(".","_") + "_" + str(merged_dict['id'])

                #Create a new record if the external ID does not exist
                if self.env['ir.model.data'].xmlid_to_res_id('odbc_import.' + external_identifier) == False:

                    #Go through each field and perform manual python transform operations
                    for import_field in self.env['migration.import.odbc.table.field'].search([('table_id','=', self.id), ('field_id','!=', False)]):

                        #Remap the values to the Odoo ones
                        for alter_value in import_field.alter_value_ids:
                            if str(merged_dict[import_field.field_id.name]) == str(alter_value.old_value):
                                merged_dict[import_field.field_id.name] = alter_value.new_value

                        #Rearrange date to fit in with Odoo date
                        if import_field.date_format:
                            external_date = datetime.strptime(merged_dict[import_field.field_id.name], import_field.date_format)
                            merged_dict[import_field.field_id.name] = external_date.strftime(DEFAULT_SERVER_DATETIME_FORMAT)
                                
                    new_rec = self.env[self.model_id.model].create(merged_dict)
                                        
                    self.env['ir.model.data'].create({'module': "odbc_import", 'name': external_identifier, 'model': self.model_id.model, 'res_id': new_rec.id })

                #TODO write update record code

                import_record_count += 1
            else:
                raise UserError("External ID is neccassary to import")

        import_log_table.imported_records = import_record_count
     
    @api.multi
    def open_line(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': "Import DB Table", 
            'view_type': 'form',
            'view_mode': 'form',
            'res_model': self._name,
            'res_id': self.id,
            'target': 'current',
        }

    @api.onchange('db_field_ids')
    def _onchange_db_field_ids(self):

        sql = ""
        sql += "SELECT "
        for import_field in self.db_field_ids:
            if import_field.field_id:
                if import_field.field_id.name != import_field.name:
                    sql += import_field.name + " as " + import_field.field_id.name + ", "
                else:
                    sql += import_field.name + ", "
            else:
                #Import the primary key but rename it to id so we can create an external id from it
                if import_field.is_key:
                    if import_field.name != 'id':
                        sql += import_field.name + " as id, "
                    else:
                        sql += "id, "
                    
        sql = sql[:-2] + " FROM " + self.name
        sql += " " + self.where_clause
        self.select_sql = sql
        
    @api.onchange('model_id')
    def _onchange_model_id(self):
        """ Try to find the corrosponding field and validate it """
        
        conn = pyodbc.connect(self.import_id.connection_string)
        odbc_cursor = conn.cursor()
        
        #Try to map the columns to ORM fields
        for column in self.db_field_ids:
            column.model_id = self.model_id.id
            if column.name != "id":
                orm_field = self.env['ir.model.fields'].search([('model_id','=',self.model_id.id), '|', ('name','=',column.name), ('name','=',column.orm_name)])
                if orm_field:
                    column.field_id = orm_field[0].id
                    column.orm_type = orm_field.ttype
                    column.validate(odbc_cursor)

    def validate(self):
        """ Validate all fields in the table """
        
        conn = pyodbc.connect(self.import_id.connection_string)
        odbc_cursor = conn.cursor()

        #Go through each column that will be imported and validate it
        for db_field in self.db_field_ids:
            if db_field.field_id:
                db_field.validate(odbc_cursor)

class MigrationImportOdbcTableDownload(models.Model):

    _name = "migration.import.odbc.table.download"

    table_id = fields.Many2one('migration.import.odbc.table', string="Database Table")
    model_id = fields.Many2one('ir.model', string="Model ID")
    field_id = fields.Many2one('ir.model.fields', string="Field", help="The ORM field that the file get imported into")
    download_url = fields.Char(string="Download URL")

    @api.onchange('field_id')
    def _onchange_field_id(self):
        self.download_url = "http://" + str(self.table_id.import_id.connect_server) + "/images/${id}.jpg"
    
class MigrationImportOdbcTableField(models.Model):

    _name = "migration.import.odbc.table.field"

    table_id = fields.Many2one('migration.import.odbc.table', string="Database Table")
    name = fields.Char(string="Name")
    orm_type = fields.Char(string="ORM Type")
    model_id = fields.Many2one('ir.model', string="Model ID", related="table_id.model_id")
    orm_name = fields.Char(string="ORM Name")
    field_id = fields.Many2one('ir.model.fields', string="Field", help="The ORM field that the data get imported into")
    is_key = fields.Boolean(string="is Primary Key")
    date_format = fields.Char(string="Date Format", help="Time format string of the import database e.g. '%Y-%m-%d'")    
    alter_value_ids = fields.One2many('migration.import.odbc.table.field.alter', 'field_id', string="Alter Values")
    valid = fields.Selection([('invalid','invalid'), ('valid','valid')], string="Valid")
            
    @api.model
    def get_field_records(self):

        import_field = self.env['migration.import.odbc.table.field'].browse( self.context['active_id'] )

        return_list = []
        
        if import_field.field_id.ttype == "selection":
            selection_list = dict(self.env[import_field.field_id.model_id.model]._columns[import_field.field_id.name].selection)
        
            for selection_value,selection_label in selection_list.items():
    	        return_list.append( (selection_value, selection_label) )
    	    
        return return_list
    
    @api.multi
    def find_distinct_values(self):
        self.ensure_one()

        conn = pyodbc.connect(self.table_id.import_id.connection_string)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) AS dis_count, " + self.name + " AS dis_value FROM " + self.table_id.name + " GROUP BY " + self.name)

        columns = [column[0] for column in cursor.description]
        results = []
        
        dist_rec = self.env['migration.import.odbc.table.field.distinct'].create({'name': self.name})
        for row in cursor.fetchall():
            
            #There are only two columns but dictionaries are always a cool way to access data
            row_dict = dict(zip(columns, row))
            dist_rec.row_ids.create({'d_id': dist_rec.id, 'dis_value': str(row_dict['dis_value']), 'dis_count': str(row_dict['dis_count']) })
            
        return {
            'type': 'ir.actions.act_window',
            'name': "Distinct Field values", 
            'view_type': 'form',
            'view_mode': 'form',
            'res_model': 'migration.import.odbc.table.field.distinct',
            'res_id': dist_rec.id,
            'target': 'new',
        }


    @api.multi
    def open_line(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': "Import DB Table", 
            'view_type': 'form',
            'view_mode': 'form',
            'res_model': self._name,
            'res_id': self.id,
            'target': 'new',
        }

    @api.multi
    def define_relationship(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': "Define Database Relationship", 
            'view_type': 'form',
            'view_mode': 'form',
            'res_model': 'migration.import.odbc.relationship',
            'context': {'default_import_id': self.table_id.import_id.id, 'default_table1': self.table_id.id, 'default_table1_id_field': self.id},
            'target': 'new',
        }
        
    def check_valid(self):
        """ Called after a person has remapped all the values """
        conn = pyodbc.connect(self.table_id.import_id.connection_string)
        odbc_cursor = conn.cursor()
            
        self.validate(odbc_cursor)        
    
    def validate(self, odbc_cursor):
        """ Each field type has it's own form of validation """
            
        method = '_validate_%s' % (self.field_id.ttype,)
        action = getattr(self, method, None)
            
        if not action:
	    _logger.error("Validation not Implemented")
	    #raise NotImplementedError('Method %r is not implemented on %r object.' % (method, self))
        else:
            self.valid = action(odbc_cursor)

    def _validate_char(self, odbc_cursor):
        """ char is super flexable so we only need to validate the length """

        #Valid until proven otherwise
        valid = "valid"
        
        if self.field_id.size > 0:
            #Get any values that exceed the length of the field
            odbc_cursor.execute("SELECT COUNT(" + self.name + ") FROM " + self.table_id.name + " WHERE LENGTH(" + self.name + ") > " + str(self.field_id.size) )

            row_count = odbc_cursor.fetchone()[0]
            if row_count > 0:
                valid = "invalid"

        return valid
        
    def _validate_selection(self, odbc_cursor):
        """ Compare the distinct values with the ones the the selection """

        #Valid until proven otherwise
        valid = "valid"
        
        #Get all distinct values and compare them to the internal values in the selection field
        odbc_cursor.execute("SELECT " + self.name + " AS dis_value FROM " + self.table_id.name + " GROUP BY " + self.name)

        columns = [column[0] for column in odbc_cursor.description]
        selection_list = dict(self.env[self.model_id.model]._fields[self.field_id.name].selection)

        for row in odbc_cursor.fetchall():
            #Convert to dictionary
            row_dict = dict(zip(columns, row))
            _logger.error(self.id)
            remap_value = self.env['migration.import.odbc.table.field.alter'].search([('field_id','=', self.id), ('old_value','=', str(row_dict['dis_value']) )])

            if not(str(row_dict['dis_value']) in selection_list.keys() or remap_value.new_value in selection_list.keys()):
                valid = "invalid"
                
                #Don't reaad the same value
                if remap_value == False:
                    self.env['migration.import.odbc.table.field.alter'].create({'field_id': self.id, 'old_value': str(row_dict['dis_value']), 'new_value': '?'})

        return valid

    def _validate_text(self, odbc_cursor):
        """ No limitations so always valid """

        return "valid"

    def _validate_integer(self, odbc_cursor):
        """ Check to see if value contains only digits """

        #valid until a non integer value is found
        valid = ""

        #Regular expression does not work...
        #odbc_cursor.execute("SELECT COUNT(" + self.name + ") FROM " + self.table_id.name + " WHERE " + self.name + " LIKE '^[0-9]+'")

        #row_count = odbc_cursor.fetchone()[0]
        #_logger.error(row_count)
        #if row_count > 0:
        #    valid = "invalid"

        return valid
        
    def _validate_many2one(self, odbc_cursor):
        """Compare the value in the field to the name field"""

        #valid until a record does not align
        valid = "valid"

        #Get all distinct values and compare them to the name value of the Odoo field
        odbc_cursor.execute("SELECT " + self.name + " AS dis_value FROM " + self.table_id.name + " GROUP BY " + self.name)

        columns = [column[0] for column in odbc_cursor.description]

        for row in odbc_cursor.fetchall():
            #Convert to dictionary
            row_dict = dict(zip(columns, row))            
            
            #We have to loop through each record to get the name
            for rec in self.env[self.field_id.relation].search([]):
                if rec.name_get()[0][1] == str(row_dict['dis_value']):   
                    #The value maps just fine so we don't need to alter it
                    #Don't readd the same value
                    if self.env['migration.import.odbc.table.field.alter'].search_count([('field_id','=', self.id), ('old_value','=', str(row_dict['dis_value']) )]) == 0:
                        self.env['migration.import.odbc.table.field.alter'].create({'field_id': self.id, 'old_value': str(row_dict['dis_value']), 'new_value': rec.id })
                else:
                    #Don't readd the same value
                    if self.env['migration.import.odbc.table.field.alter'].search_count([('field_id','=', self.id), ('old_value','=', str(row_dict['dis_value']) )]) == 0:
                        self.env['migration.import.odbc.table.field.alter'].create({'field_id': self.id, 'old_value': str(row_dict['dis_value']), 'new_value': '?'})
                       
                    valid = "invalid"

        return valid
                
    def auto_create_field(self):
        if is_key == False:
            new_field = self.env['ir.model.fields'].create({'ttype': self.orm_type, 'name': self.orm_name, 'field_description':self.name, 'model_id':self.table_id.model_id.id})
            self.field_id = new_field.id

class MigrationImportOdbcTableFieldAlter(models.Model):

    _name = "migration.import.odbc.table.field.alter"

    field_id = fields.Many2one('migration.import.odbc.table.field', string="Field")
    old_value = fields.Char(string="Old Value")
    new_value = fields.Char(string="New Value")
    
    def auto_create_record(self):
        if self.new_value != "":
            new_record = self.env[self.field_id.field_id.relation].create({'name': self.old_value})
            self.new_value = new_record.id    
            
class MigrationImportOdbcTableFieldDistinct(models.Model):

    _name = "migration.import.odbc.table.field.distinct"

    name = fields.Char(string="Name")
    row_ids = fields.One2many('migration.import.odbc.table.field.distinct.row','d_id', string="Rows")

class MigrationImportOdbcTableFieldDistinctRow(models.TransientModel):

    _name = "migration.import.odbc.table.field.distinct.row"

    d_id = fields.Many2one('migration.import.odbc.table.field.distinct', string="Distinct Field")
    dis_value = fields.Char(string="Distinct Value")
    dis_count = fields.Char(string="Distinct Count")
    
class MigrationImportOdbcTableDefault(models.Model):

    _name = "migration.import.odbc.table.default"

    table_id = fields.Many2one('migration.import.odbc.table', string="Database Table")
    model_id = fields.Many2one('ir.model', string="Model ID", related="table_id.model_id")
    field_id = fields.Many2one('ir.model.fields', string="ORM Field")
    value = fields.Char(string="Value")
    
