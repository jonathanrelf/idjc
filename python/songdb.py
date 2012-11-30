#   songdb.py: music database connectivity
#   Copyright (C) 2012 Stephen Fairchild (s-fairchild@users.sourceforge.net)
#             (C) 2012 Brian Millham (bmillham@users.sourceforge.net)
#
#   This program is free software: you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation, either version 2 of the License, or
#   (at your option) any later version.
#
#   This program is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with this program in the file entitled COPYING.
#   If not, see <http://www.gnu.org/licenses/>.


import time
import types
import gettext
import threading
from functools import partial, wraps
from collections import deque
from urllib import quote

import glib
import pango
import gtk
try:
    import MySQLdb as sql
except ImportError:
    sql = None

from idjc import FGlobs
from .tooltips import set_tip
from .gtkstuff import threadslock, DefaultEntry


__all__ = ['MediaPane']

AMPACHE = "Ampache"
PROKYON_3 = "Prokyon 3"
FUZZY, CLEAN, WHERE, DIRTY = xrange(4)

t = gettext.translation(FGlobs.package_name, FGlobs.localedir, fallback=True)
_ = t.gettext


def thread_only(func):
    """Guard a method from being called from outside the thread context."""
    
    @wraps(func)
    def inner(self, *args, **kwargs):
        assert threading.current_thread() == self
        func(self, *args, **kwargs)
    return inner


class DBAccessor(threading.Thread):
    """A class to hide the intricacies of database access.
    
    When the database connection is dropped due to timeout it will silently 
    remake the connection and continue on with its work.
    """
    
    def __init__(self, hostnameport, user, password, database, notify):
        """The notify function must lock gtk before accessing widgets."""
        
        threading.Thread.__init__(self)
        try:
            hostname, port = hostnameport.rsplit(":", 1)
            port = int(port)
        except ValueError:
            hostname = hostnameport
            port = 3306  # MySQL uses this as the default port.

        self.hostname = hostname
        self.port = port
        self.user = user
        self.password = password
        self.database = database
        self.notify = notify
        self._handle = None  # No connections made until there is a query.
        self._cursor = None
        self.jobs = deque()
        self.semaphore = threading.Semaphore()
        self.keepalive = True
        self.start()

    def request(self, sql_query, handler, failhandler=None):
        """Add a request to the job queue."""
        
        self.jobs.append((sql_query, handler, failhandler))
        self.semaphore.release()

    def close(self):
        """Clean up the worker thread prior to disposal."""
        
        if self.is_alive():
            self.keepalive = False
            self.semaphore.release()
            return

    def run(self):
        """This is the worker thread."""

        notify = partial(glib.idle_add, threadslock(self.notify))
        
        try:
            while self.keepalive:
                self.semaphore.acquire()
                if self.keepalive and self.jobs:
                    query, handler, failhandler = self.jobs.popleft()

                    trycount = 0
                    while trycount < 3:
                        try:
                            if query is None:
                                self.disconnect()
                            
                            rows = self._cursor.execute(*query)
                        except (sql.Error, AttributeError) as e:
                            if not self.keepalive:
                                return
                            
                            if isinstance(e, sql.OperationalError):
                                # Unhandled errors will be treated like
                                # connection failures.
                                try:
                                    self._cursor.close()
                                except Exception:
                                    pass
                                    
                                try:
                                    self._handle.close()
                                except Exception:
                                    pass
                                
                            if not self.keepalive:
                                return

                            notify(_('Connecting'))
                            trycount += 1
                            try:
                                self._handle = sql.Connection(
                                    host=self.hostname, port=self.port,
                                    user=self.user, passwd=self.password,
                                    db=self.database, connect_timeout=6)
                                self._cursor = self._handle.cursor()
                            except sql.Error as e:
                                notify(_("Connection failed (try %d)") %
                                                                    trycount)
                                print e
                                time.sleep(0.5)
                            else:
                                try:
                                    self._cursor.execute('set names utf8')
                                    self._cursor.execute(
                                                    'set character set utf8')
                                    self._cursor.execute(
                                            'set character_set_connection=utf8')
                                except sql.MySQLError:
                                    notify(_('Connected: utf-8 mode failed'))
                                else:
                                    notify(_('Connected'))
                        else:
                            if not self.keepalive:
                                return
                            handler(self, self.request, self._cursor, notify,
                                                                        rows)
                            break
                    else:
                        if failhandler is not None:
                            failhandler(e, notify)
                        else:
                            notify(_('Job dropped'))
        finally:
            try:
                self._cursor.close()
            except Exception:
                pass
            try:
                self._handle.close()
            except Exception:
                pass
            notify(_('Disconnected'))

    @thread_only
    def purge_job_queue(self, remain=0):
        while len(self.jobs) > remain:
            self.jobs.popleft()
            self.semaphore.acquire()

    @thread_only
    def disconnect(self):
        try:
            self._handle.close()
        except sql.Error:
            glib.idle_add(threadslock(self.notify),
                                            _('Problem dropping connection'))
        else:
            glib.idle_add(threadslock(self.notify), _('Connection dropped'))

    @thread_only
    def replace_cursor(self, cursor):
        """Handler may break off the cursor to pass along its data."""
        
        assert cursor is self._cursor
        self._cursor = self._handle.cursor()


class PrefsControls(gtk.Frame):
    """Database controls as visible in the preferences window."""
    
    def __init__(self):
        gtk.Frame.__init__(self)
        self.set_border_width(3)
        label = gtk.Label(" %s " % 
                            _('Prokyon3 or Ampache (song title) Database'))
        set_tip(label, _('You can make certain media databases accessible in '
                            'IDJC for easy drag and drop into the playlists.'))
        self.set_label_widget(label)
        
        # List of widgets that should be made insensitive when db is active. 
        self._parameters = []  
        if not sql:
            # Feature is disabled.
            vbox = gtk.VBox()
            vbox.set_sensitive(False)
            vbox.set_border_width(3)
            label = gtk.Label(_('Python module MySQLdb required'))
            vbox.add(label)
            self.add(vbox)
            self.data_panel = gtk.VBox()  # Empty placeholder widget.
        else:
            # Control widgets.
            table = gtk.Table(5, 4)
            table.set_border_width(10)
            table.set_row_spacings(1)
            for col, spc in zip(xrange(3), (3, 10, 3)):
                table.set_col_spacing(col, spc)

            # Attachment for labels.
            l_attach = partial(table.attach, xoptions=gtk.SHRINK | gtk.FILL)
            
            # Top row.
            hostportlabel, self._hostnameport = self._factory(
                                            _('Hostname[:Port]'), 'localhost')
            l_attach(hostportlabel, 0, 1, 0, 1)
            table.attach(self._hostnameport, 1, 4, 0, 1)
            
            # Second row.
            hbox = gtk.HBox()
            hbox.set_spacing(3)
            fpmlabel, self._addchars = self._factory(
                                                _('File Path Modify'), None)
            adj = gtk.Adjustment(0.0, 0.0, 999.0, 1.0, 1.0)
            self._delchars = gtk.SpinButton(adj, 0.0, 0)
            self._parameters.append(self._delchars)
            set_tip(self._delchars, _('The number of characters to strip from '
                                    'the left hand side of media file paths.'))
            set_tip(self._addchars, 
                        _('The characters to prefix to the media file paths.'))
            l_attach(fpmlabel, 0, 1, 1, 2)
            minus = gtk.Label('-')
            hbox.pack_start(minus, False)
            hbox.pack_start(self._delchars, False)
            plus = gtk.Label('+')
            hbox.pack_start(plus, False)
            hbox.pack_start(self._addchars)
            table.attach(hbox, 1, 4, 1, 2)
            
            # Third row.
            userlabel, self._user = self._factory(_('User Name'), "admin")
            l_attach(userlabel, 0, 1, 3, 4)
            table.attach(self._user, 1, 2, 3, 4)
            dblabel, self._database = self._factory(_('Database'), "ampache")
            l_attach(dblabel, 2, 3, 3, 4)
            table.attach(self._database, 3, 4, 3, 4)
            
            # Fourth row.
            passlabel, self._password = self._factory(_('Password'), "")
            self._password.set_visibility(False)
            l_attach(passlabel, 0, 1, 4, 5)
            table.attach(self._password, 1, 2, 4, 5)
            self.dbtoggle = gtk.ToggleButton(_('Music Database'))
            self.dbtoggle.set_size_request(10, -1)
            self.dbtoggle.connect("toggled", self._cb_dbtoggle)
            table.attach(self.dbtoggle, 2, 4, 4, 5)
            
            # Notification row.
            self._statusbar = gtk.Statusbar()
            self._statusbar.set_has_resize_grip(False)
            cid = self._statusbar.get_context_id("all output")
            self._statusbar.push(cid, _('Disconnected'))
            table.attach(self._statusbar, 0, 4, 5, 6)
            
            self.add(table)
            self.data_panel = gtk.VBox()  # Bring in widget at some point.
            
        self.data_panel.set_no_show_all(False)
        self.show_all()
        
        # Save and Restore settings.
        self.activedict = {"songdb_active": self.dbtoggle}
        self.valuesdict = {"songdb_delchars": self._delchars}
        self.textdict = {"songdb_hostnameport": self._hostnameport,
            "songdb_user": self._user, "songdb_password": self._password,
            "songdb_dbname": self._database, "songdb_addchars": self._addchars}
        
    def disconnect(self):
        self.dbtoggle.set_active(False)    
        
    def bind(self, callback):
        """Connect with the activate method of the view pane."""
        
        self.dbtoggle.connect("toggled", self._cb_bind, callback)

    def _cb_bind(self, widget, callback):
        """This runs when the database is toggled on and off."""
        
        if widget.get_active():
            # Collate parameters for DBAccessor contructors.
            conn_data = {"notify": self._notify}
            for key in "hostnameport user password database".split():
                conn_data[key] = getattr(self, "_" + key).get_text().strip()
            
            # Make a file path transformation function.
            del_qty = self._delchars.get_value_as_int()
            prepend_str = self._addchars.get_text().strip()

            if del_qty or prepend_str:
                def transform(input_str):
                    return prepend_str + input_str[del_qty:]
            else:
                transform = lambda s: s  # Do nothing.
        else:
            conn_data = transform = None

        callback(conn_data, transform)

    def _cb_dbtoggle(self, widget):
        """Parameter widgets to be made insensitive when db is active."""
    
        sens = not widget.get_active()
    
        for each in self._parameters:
            each.set_sensitive(sens)

    def _notify(self, message):
        """Display status messages beneath the prefs settings."""
        
        print "Song title database:", message
        cid = self._statusbar.get_context_id("all output")
        self._statusbar.pop(cid)
        self._statusbar.push(cid, message)
        # To ensure readability of long messages also set the tooltip.
        self._statusbar.set_tooltip_text(message)

    def _factory(self, labeltext, entrytext=None):
        """Widget factory method."""
        
        label = gtk.Label(labeltext)
        label.set_alignment(1.0, 0.5)
        
        if entrytext:
            entry = DefaultEntry(entrytext, True)
        else:
            entry = gtk.Entry()
            
        entry.set_size_request(10, -1)
        self._parameters.append(entry)
        return label, entry


class PageCommon(gtk.VBox):
    """Base class for TreePage and FlatPage."""
    
    def __init__(self, notebook, label_text, controls):
        gtk.VBox.__init__(self)
        self.set_spacing(2)
        self.scrolled_window = gtk.ScrolledWindow()
        self.scrolled_window.set_policy(gtk.POLICY_AUTOMATIC, gtk.POLICY_ALWAYS)
        self.pack_start(self.scrolled_window)
        self.tree_view = gtk.TreeView()
        self.tree_view.set_enable_search(False)
        self.tree_view.set_rubber_banding(True)
        self.tree_selection = self.tree_view.get_selection()
        self.tree_selection.set_mode(gtk.SELECTION_MULTIPLE)
        self.scrolled_window.add(self.tree_view)
        self.pack_start(controls, False)
        label = gtk.Label(label_text)
        notebook.append_page(self, label)
        
        self.tree_view.enable_model_drag_source(gtk.gdk.BUTTON1_MASK,
            self._sourcetargets, gtk.gdk.ACTION_DEFAULT | gtk.gdk.ACTION_COPY)
        self.tree_view.connect_after("drag-begin", self._cb_drag_begin)
        self.tree_view.connect("drag-data-get", self._cb_drag_data_get)
        self._update_id = deque()
        self._acc = None

    @property
    def db_type(self):
        return self._db_type

    @property
    def transform(self):
        return self._transform

    def get_col_widths(self):
        pass

    def get_col_widths(self):
        return ",".join([str(x.get_width() or x.get_fixed_width())
                                                    for x in self.tree_cols])

    def set_col_widths(self, data):
        """Restore column width values."""
         
        c = self.tree_cols.__iter__()
        for w in data.split(","):
            if w != "0":
                c.next().set_fixed_width(int(w))
            else:
                c.next()

    def activate(self, accessor, db_type, transform):
        self._db_type = db_type
        self._acc = accessor
        self._transform = transform
        
    def deactivate(self):
        while self._update_id:
            context, namespace = self._update_id.popleft()
            namespace[0] = True
            glib.source_remove(context)
        
        self._acc = None
        model = self.tree_view.get_model()
        self.tree_view.set_model(None)
        if model is not None:
            model.clear()

    def repair_focusability(self):
        self.tree_view.set_flags(gtk.CAN_FOCUS)

    _sourcetargets = (  # Drag and drop source target specs.
        ('text/plain', 0, 1),
        ('TEXT', 0, 2),
        ('STRING', 0, 3))

    def _cb_drag_begin(self, widget, context):
        """Set icon for drag and drop operation."""

        context.set_icon_stock(gtk.STOCK_CDROM, -5, -5)

    def _cb_drag_data_get(self, tree_view, context, selection, target, etime):
        model, paths = self.tree_selection.get_selected_rows()
        data = ("file://%s/%s" % row for row in self._drag_data(model, paths))
        selection.set(selection.target, 8, "\n".join(data))

    @staticmethod
    def _make_tv_columns(tree_view, parameters):
        """Build a TreeViewColumn list from a table of data."""

        list_ = []
        for label, data_index, data_function, mw, el in parameters:
            renderer = gtk.CellRendererText()
            renderer.props.ellipsize = el
            column = gtk.TreeViewColumn(label, renderer)
            column.add_attribute(renderer, 'text', data_index)
            if mw != -1:
                column.set_resizable(True)
                column.set_sizing(gtk.TREE_VIEW_COLUMN_FIXED)
                column.set_min_width(mw)
                column.set_fixed_width(mw + 50)
            tree_view.append_column(column)
            list_.append(column)
            if data_function is not None:
                column.set_cell_data_func(renderer, data_function, data_index)
        return list_

    def _cond_cell_secs_to_h_m_s(self, column, renderer, model, iter, cell):
        if model.get_value(iter, 0) >= 0:
            return self._cell_secs_to_h_m_s(column, renderer, model, iter, cell)
        else:
            renderer.set_property("text", "")
    
    def _cell_k(self, column, renderer, model, iter, cell):
        bitrate = model.get_value(iter, cell)
        if bitrate == 0:
            renderer.set_property("text", "")
        elif self._db_type == "P3":
            renderer.set_property("text", "%dk" % bitrate)
        elif bitrate > 9999 and self._db_type == AMPACHE:
            renderer.set_property("text", "%dk" % (bitrate // 1000))
        renderer.set_property("xalign", 1.0)
    
    @staticmethod
    def _cell_show_unknown(column, renderer, model, iter, cell):
        if model.get_value(iter, cell) == "":
            # TC: Placeholder for unknown data.
            renderer.set_property("text", _('<unknown>'))
    
    @staticmethod
    def _cell_secs_to_h_m_s(column, renderer, model, iter, cell):
        v_in = model.get_value(iter, cell)
        m, s = divmod(v_in, 60)
        h, m = divmod(m, 60)
        d, h = divmod(h, 24)
        if d:
            v_out = "%dd:%02d:%02d" % (d, h, m)
        else:
            if h:
                v_out = "%d:%02d:%02d" % (h, m, s)
            else:
                v_out = "%d:%02d" % (m, s)
        renderer.set_property("xalign", 1.0)
        renderer.set_property("text", v_out)
        
    @staticmethod
    def _cell_ralign(column, renderer, model, iter, cell):
        val = model.get_value(iter, cell)
        if val:
            renderer.set_property("xalign", 1.0)
        else:
            renderer.set_property("text", "")

    def _handler(self, acc, request, cursor, notify, rows):
        # Lock against the very start of the update functions.
        gtk.gdk.threads_enter()
        while self._update_id:
            context, namespace = self._update_id.popleft()
            glib.source_remove(context)
            # Idle functions to receive the following and know to clean-up.
            namespace[0] = True
        gtk.gdk.threads_leave()

        try:
            self._old_cursor.close()
        except sql.Error as e:
            print str(e)
        except AttributeError:
            pass

        self._old_cursor = cursor
        acc.replace_cursor(cursor)
        # Scrap intermediate jobs whose output would merely slow down the
        # user interface responsiveness.
        namespace = [False, ()]
        context = glib.idle_add(self._update_1, acc, cursor, rows, namespace)
        self._update_id.append((context, namespace))


class ExpandAllButton(gtk.Button):
    def __init__(self, expanded, tooltip=None):
        expander = gtk.Expander()
        expander.set_expanded(expanded)
        expander.show_all()
        gtk.Button.__init__(self)
        self.add(expander)
        if tooltip is not None:
            set_tip(self, tooltip)


class TreePage(PageCommon):
    """Tree UI with Artist, Album, Title heirarchy."""

    def __init__(self, notebook):
        # Base class overwrites these values.
        self.scrolled_window = self.tree_view = self.tree_selection = None
        self.transfrom = self.db_accessor = None

        self.controls = gtk.HBox()
        layout_store = gtk.ListStore(str, gtk.TreeStore)
        self.layout_combo = gtk.ComboBox(layout_store)
        cell_text = gtk.CellRendererText()
        self.layout_combo.pack_start(cell_text)
        self.layout_combo.add_attribute(cell_text, "text", 0)
        self.controls.pack_start(self.layout_combo, False)
        self.right_controls = gtk.HBox()
        self.right_controls.set_spacing(1)
        self.tree_rebuild = gtk.Button()
        set_tip(self.tree_rebuild, _('Reload the database.'))
        image = gtk.image_new_from_stock(gtk.STOCK_REFRESH, gtk.ICON_SIZE_MENU)
        self.tree_rebuild.add(image)
        self.tree_rebuild.connect("clicked", self._cb_tree_rebuild)
        self.tree_rebuild.set_use_stock(True)
        tree_expand = ExpandAllButton(True, _('Expand entire tree.'))
        tree_collapse = ExpandAllButton(False, _('Collapse tree.'))
        sg = gtk.SizeGroup(gtk.SIZE_GROUP_HORIZONTAL)
        for each in (self.tree_rebuild, tree_expand, tree_collapse):
            self.right_controls.pack_start(each, False)
            sg.add_widget(each)
        self.controls.pack_end(self.right_controls, False)

        PageCommon.__init__(self, notebook, _('Browse'), self.controls)
        
        self.tree_view.set_enable_tree_lines(True)
        self.tree_selection.set_select_function(self._tree_select_func)

        tree_expand.connect_object("clicked", gtk.TreeView.expand_all,
                                                                self.tree_view)
        tree_collapse.connect_object("clicked", gtk.TreeView.collapse_all,
                                                                self.tree_view)
        self.tree_cols = self._make_tv_columns(self.tree_view, (
                ("", 1, self._cell_show_unknown, 180, pango.ELLIPSIZE_END),
                # TC: The disk number of the album track.
                (_('Disk'), 7, self._cell_ralign, -1, pango.ELLIPSIZE_NONE),
                # TC: The album track number.
                (_('Track'), 2, self._cell_ralign, -1, pango.ELLIPSIZE_NONE),
                # TC: Track playback time.
                (_('Duration'), 3, self._cond_cell_secs_to_h_m_s, -1, pango.ELLIPSIZE_NONE),
                (_('Bitrate'), 4, self._cell_k, -1, pango.ELLIPSIZE_NONE),
                (_('Filename'), 5, None, 100, pango.ELLIPSIZE_END),
                # TC: Directory path to a file.
                (_('Path'), 6, None, -1, pango.ELLIPSIZE_NONE),
                ))

        # depth, ARTIST-ALBUM-TITLE, TRACK, DURATION, BITRATE, filename,
        # path, disk
        data_signature = int, str, int, int, int, str, str, int
        self.artist_store = gtk.TreeStore(*data_signature)
        self.album_store = gtk.TreeStore(*data_signature + (str, ))
        self.album_store.set_default_sort_func(self._album_sort_compare)
        layout_store.append((_('Artist - Album - Track'), self.artist_store))
        layout_store.append((_('Album - Artist - Track'), self.album_store))
        self.layout_combo.set_active(0)
        self.layout_combo.connect("changed", self._cb_layout_combo)

        self.loading_vbox = gtk.VBox()
        self.loading_vbox.set_border_width(20)
        self.loading_vbox.set_spacing(20)
        # TC: The database tree view is being built (populated).
        self.loading_label = gtk.Label()
        self.loading_vbox.pack_start(self.loading_label, False)
        self.progress_bar = gtk.ProgressBar()
        self.loading_vbox.pack_start(self.progress_bar, False)
        self.pack_start(self.loading_vbox)
        self._pulse_id = deque()
        
        self.show_all()

    def set_loading_view(self, loading):
        if loading:
            self.progress_bar.set_fraction(0.0)
            self.loading_label.set_text(_('Fetching'))
            self.controls.hide()
            self.scrolled_window.hide()
            self.loading_vbox.show()
        else:
            self.layout_combo.emit("changed")
            self.loading_vbox.hide()
            self.scrolled_window.show()
            self.controls.show()

    def activate(self, *args, **kwargs):
        PageCommon.activate(self, *args, **kwargs)
        glib.idle_add(threadslock(self.tree_rebuild.clicked))

    def deactivate(self):
        while self._pulse_id:
            glib.source_remove(self._pulse_id.popleft())
        
        PageCommon.deactivate(self)

    @staticmethod
    def _album_sort_compare(model, i1, i2):
        if model.get_value(i1, 0) < 0:
            return cmp(model.get(i1, 1, 8), model.get(i2, 1, 8))
        else:
            return cmp(model.get(i1, 7, 2, 1), model.get(i2, 7, 2, 1))

    def _cb_layout_combo(self, widget):
        store = widget.get_model().get_value(widget.get_active_iter(), 1)
        self.tree_view.set_model(store)

    def _cb_tree_rebuild(self, widget):
        """(Re)load the tree with info from the database."""

        self.set_loading_view(True)
        if self._db_type == PROKYON_3:
            query = """SELECT artist,album,tracknumber,title,length,bitrate,
                        filename,path,0 as disk FROM tracks ORDER BY
                        artist,album,path,tracknumber,title"""
        elif self._db_type == AMPACHE:
            query = """SELECT
                concat_ws(" ", artist.prefix, artist.name) as artist, 
                concat_ws(" ", album.prefix, album.name) as album,
                track as tracknumber, title, time as length, 
                bitrate, file, "" as padding, album.disk as disk
                from song
                left join artist on song.artist = artist.id 
                left join album on song.album = album.id 
                ORDER BY artist.name,album.disk,album.name,tracknumber,title"""
        else:
            print "unsupported database type:", self._db_type
            return
            
        self._pulse_id.append(glib.timeout_add(1000, self._progress_pulse))
        self._acc.request((query,), self._handler, self._failhandler)

    @staticmethod
    def _tree_select_func(info):
        return len(info) - 1

    @staticmethod
    def _drag_data(model, paths):
        if len(paths) == 1 and len(paths[0]) == 2:
            d2 = 0
            while 1:
                try:
                    row = model[paths[0] + (d2, )]
                except:
                    return
                yield row[6], row[5]
                d2 += 1
        else:
            for each in paths:
                if len(each) == 3:
                    row = model[each]
                    yield row[6], row[5]

    @threadslock
    def _progress_pulse(self):
        self.progress_bar.pulse()
        return True

    ###########################################################################

    def _handler(self, acc, request, cursor, notify, rows):
        PageCommon._handler(self, acc, request, cursor, notify, rows)
        acc.disconnect()

    def _failhandler(self, exception, notify):
        print str(exception)
        notify(_('Tree fetch failed'))
        glib.idle_add(threadslock(self.loading_label.set_text),
                                                        _('Fetch Failed!'))
        while self._pulse_id:
            glib.source_remove(self._pulse_id.popleft())

    ###########################################################################

    @threadslock
    def _update_1(self, acc, cursor, rows, namespace):
        if namespace[0]:
            return False
            
        self.loading_label.set_text(_('Populating'))
        # Turn off progress bar pulser.
        while self._pulse_id:
            glib.source_remove(self._pulse_id.popleft())

        # Clean away old data.
        self.tree_view.set_model(None)
        self.artist_store.clear()
        self.album_store.clear()

        namespace = [False, (0, 0, 0.0)]  # r_dep, l_dep, done 
        do_max = min(max(30, rows / 100), 200)  # Data size to process.
        total = float(rows)
        context = glib.idle_add(self._update_2, acc, cursor, total, do_max,
                                                                    namespace)
        self._update_id.append((context, namespace))
        return False

    @threadslock
    def _update_2(self, acc, cursor, total, do_max, namespace):
        kill, (r_dep, l_dep, done) = namespace
        if kill:
            return False

        transform = self.transform
        ampache = self.db_type == AMPACHE
        r_append = self.artist_store.append
        l_append = self.album_store.append
        next_row = cursor.fetchone

        for each in xrange(do_max):
            if acc.keepalive == False:
                return False

            row = next_row()
            if row is None:
                if acc.keepalive:
                    self.loading_label.set_text(_('Sorting'))
                    self.album_store.set_sort_column_id(-1, gtk.SORT_ASCENDING)
                    self.album_store.set_sort_column_id(0, gtk.SORT_ASCENDING)
                    self.loading_label.set_text(_('Merging Albums'))
                    self.progress_bar.set_fraction(1.0)
                    
                    namespace = [False, (self.album_store.get_iter_root(), )]
                    context = glib.idle_add(self._update_3, acc, namespace)
                    self._update_id.append((context, namespace))
                return False
            else:
                if ampache:
                    # Split the full path into path and file.
                    try:
                        path, filename = tuple(row[6].rsplit("/", 1))
                    except ValueError:
                        continue
                        
                    row = row[0:6] + (filename, path) + row[8:]

                while 1:
                    if r_dep == 0:
                        art = row[0]
                        self.artlower = art.lower()
                        self.r_iter1 = r_append(
                                    None, (-1, art, 0, 0, 0, "", "", 0)) 
                        r_dep = 1
                    if r_dep == 1:
                        if self.artlower == row[0].lower():
                            alb = row[1]
                            self.alblower = alb.lower()
                            self.r_iter2 = r_append(
                                self.r_iter1, (-2, alb, 0, 0, 0, "", "", 0)) 
                            r_dep = 2
                        else:
                            r_dep = 0
                    if r_dep == 2:
                        if self.artlower == row[0].lower() and self.alblower \
                                                            == row[1].lower():
                            path = transform(row[7]) 
                            r_append(self.r_iter2, (0, row[3], row[2],
                                        row[4], row[5], row[6], path, row[8]))
                            break
                        else:
                            r_dep = 1

                while 1:
                    if l_dep == 0:
                        self.albartlower = row[1].lower(), row[0].lower()
                        self.l_iter = l_append(
                            None, (-1, row[1], 0, 0, 0, "", "", 0, row[0])) 
                        self.l_iter = l_append(
                            self.l_iter, (-2, row[0], 0, 0, 0, "", "", 0, "")) 
                        l_dep = 1
                    if l_dep == 1:
                        if self.albartlower == (row[1].lower(), row[0].lower()):
                            path = transform(row[7]) 
                            l_append(self.l_iter, (0, row[3], row[2],
                                    row[4], row[5], row[6], path, row[8], ""))
                            break
                        else:
                            l_dep = 0

        done += do_max
        self.progress_bar.set_fraction(done / total)
        namespace[1] = r_dep, l_dep, done
        return True

    @threadslock
    def _update_3(self, acc, namespace):
        kill, (i1, ) = namespace
        if kill:
            return False

        count = 0
        model = self.album_store
        read_alb = lambda iter: model.get_value(iter, 1).lower()
        
        while i1 is not None:
            if count == 200:
                namespace[1] = (i1, )
                return acc.keepalive
            count += 1

            alb = read_alb(i1)
            i2 = model.iter_next(i1)
            while i2 is not None and alb == read_alb(i2):
                self._copy_children(model, i1, i2)
                model.remove(i2)
            i1 = i2

        self.set_loading_view(False)
        return False

    def _copy_children(this, model, p1, p2):
        c2 = model.iter_children(p2)
        if c2 is None:
            return

        while c2:
            c1 = model.append(p1, model.get(c2, *xrange(9)))
            this(this, model, c1, c2)
            c2 = model.iter_next(c2)
    _copy_children = types.MethodType(_copy_children, _copy_children)


class FlatPage(PageCommon):
    """Flat list based user interface with a search facility."""
    
    def __init__(self, notebook):
        # Base class overwrites these values.
        self.scrolled_window = self.tree_view = self.tree_selection = None
        self.transfrom = self.db_accessor = None

        # TC: User specified search filter entry box title text.
        self.controls = gtk.Frame(" %s " % _('Filters'))
        self.controls.set_shadow_type(gtk.SHADOW_OUT)
        self.controls.set_border_width(1)
        self.controls.set_label_align(0.5, 0.5)
        filter_vbox = gtk.VBox()
        filter_vbox.set_border_width(3)
        filter_vbox.set_spacing(1)
        self.controls.add(filter_vbox)
        
        fuzzy_hbox = gtk.HBox()
        filter_vbox.pack_start(fuzzy_hbox, False)
        # TC: A type of search on any data field matching paritial strings.
        fuzzy_label = gtk.Label(_('Fuzzy Search'))
        fuzzy_hbox.pack_start(fuzzy_label, False)
        self.fuzzy_entry = gtk.Entry()
        self.fuzzy_entry.connect("changed", self._cb_fuzzysearch_changed)
        fuzzy_hbox.pack_start(self.fuzzy_entry, True, True, 0)
        
        where_hbox = gtk.HBox()
        filter_vbox.pack_start(where_hbox, False)
        # TC: WHERE is an SQL keyword.
        where_label = gtk.Label(_('WHERE'))
        where_hbox.pack_start(where_label, False)
        self.where_entry = gtk.Entry()
        self.where_entry.connect("activate", self._cb_update)
        where_hbox.pack_start(self.where_entry)
        image = gtk.image_new_from_stock(gtk.STOCK_EXECUTE,
                                                        gtk.ICON_SIZE_BUTTON)
        self.update_button = gtk.Button()
        self.update_button.connect("clicked", self._cb_update)
        self.update_button.set_image(image)
        image.show
        where_hbox.pack_start(self.update_button, False)
        
        PageCommon.__init__(self, notebook, _("Search"), self.controls)
 
        # Row data specification:
        # index, ARTIST, ALBUM, TRACKNUM, TITLE, DURATION, BITRATE,
        # path, filename, disk
        self.list_store = gtk.ListStore(
                        int, str, str, int, str, int, int, str, str, int)
        self.tree_cols = self._make_tv_columns(self.tree_view, (
            ("(0)", 0, self._cell_ralign, -1, pango.ELLIPSIZE_NONE),
            (_('Artist'), 1, self._cell_show_unknown, 100, pango.ELLIPSIZE_END),
            (_('Album'), 2, self._cell_show_unknown, 100, pango.ELLIPSIZE_END),
            (_('Disk'), 9, self._cell_ralign, -1, pango.ELLIPSIZE_NONE),
            (_('Track'), 3, self._cell_ralign, -1, pango.ELLIPSIZE_NONE),
            (_('Title'), 4, self._cell_show_unknown, 100, pango.ELLIPSIZE_END),
            (_('Duration'), 5, self._cell_secs_to_h_m_s, -1, pango.ELLIPSIZE_NONE),
            (_('Bitrate'), 6, self._cell_k, -1, pango.ELLIPSIZE_NONE),
            (_('Filename'), 7, None, 100, pango.ELLIPSIZE_END),
            (_('Path'), 8, None, -1, pango.ELLIPSIZE_NONE),
            ))

        self.tree_view.set_rules_hint(True)

    def deactivate(self):
        self.fuzzy_entry.set_text("")
        self.where_entry.set_text("")
        PageCommon.deactivate(self)

    def repair_focusability(self):
        PageCommon.repair_focusability(self)
        self.fuzzy_entry.set_flags(gtk.CAN_FOCUS)
        self.where_entry.set_flags(gtk.CAN_FOCUS)

    _queries_table = {
        PROKYON_3:
            {FUZZY: (CLEAN, """
                    SELECT artist,album,tracknumber,title,length,
                    bitrate,filename,path,0 as disk FROM tracks
                    WHERE MATCH (artist,album,title,filename) AGAINST (%s)
                    """),
        
            WHERE: (DIRTY, """
                    SELECT artist,album,tracknumber,title,length,
                    bitrate,filename, path, 0 as disk FROM tracks WHERE (%s)
                    ORDER BY artist,album,path,tracknumber,title
                    """)},
        
        AMPACHE:
            {FUZZY: (CLEAN, """
                    SELECT
                    concat_ws(" ",artist.prefix,artist.name),
                    concat_ws(" ",album.prefix,album.name),
                    track as tracknumber, title, time as length,
                    bitrate, file, "" as padding, album.disk as disk FROM song
                    LEFT JOIN artist ON artist.id = song.artist
                    LEFT JOIN album ON album.id = song.album
                    WHERE
                         (MATCH(album.name) against(%s)
                          OR MATCH(artist.name) against(%s)
                          OR MATCH(title) against(%s))
                    """),

            WHERE: (DIRTY, """
                    SELECT
                    concat_ws(" ", artist.prefix, artist.name) as artist,
                    concat_ws(" ", album.prefix, album.name) as albumname,
                    track as tracknumber, title,
                    time as length, bitrate, file, "" as padding,
                    album.disk as disk FROM song
                    LEFT JOIN album on album.id = song.album
                    LEFT JOIN artist on artist.id = song.artist
                    WHERE (%s) ORDER BY
                    artist.name, album.name, file, album.disk, track, title
                    """)}
    }

    def _cb_update(self, widget):
        try:
            table = self._queries_table[self._db_type]
        except KeyError:
            print "unsupported database type"
            return

        user_text = self.fuzzy_entry.get_text().strip()
        if user_text:
            access_mode, query = table[FUZZY]
        else:
            access_mode, query = table[WHERE]
            user_text = self.where_entry.get_text().strip()
            if not user_text:
                self.where_entry.set_text("")
                while self._update_id:
                    context, namespace = self._update_id.popleft()
                    glib.source_remove(context)
                    namespace[0] = True
                self.list_store.clear()
                return

        qty = query.count("(%s)")
        if access_mode == CLEAN:
            query = (query, (user_text, ) * qty)
        elif access_mode == DIRTY:  # Accepting of SQL code in user data.
            query = (query % (user_text, ) * qty, )
        else:
            print "unknown database access mode", access_mode
            return

        self._acc.request(query, self._handler)
        return
            
    @staticmethod
    def _drag_data(model, paths):
        """Generate tuples of (path, filename) for the given paths."""
        
        for path in paths:
            row = model[path]
            yield row[8], row[7]

    def _cb_fuzzysearch_changed(self, widget):
        if widget.get_text().strip():
            self.where_entry.set_sensitive(False)
            self.where_entry.set_text("")
        else:
            self.where_entry.set_sensitive(True)
        self.update_button.clicked()
        
    ###########################################################################

    def _handler(self, acc, *args, **kwargs):
        PageCommon._handler(self, acc, *args, **kwargs)
        acc.purge_job_queue(1)

    ###########################################################################
    
    @threadslock
    def _update_1(self, acc, cursor, rows, namespace):
        if not namespace[0]:
            self.tree_view.set_model(None)
            self.list_store.clear()
            namespace[1] = (0, )  # found = 0
            context = glib.idle_add(self._update_2, acc, cursor, namespace)
            self._update_id.append((context, namespace))
        return False

    @threadslock
    def _update_2(self, acc, cursor, namespace):
        kill, (found, ) = namespace
        if kill:
            return False
        
        next_row = cursor.fetchone
        ampache = self._db_type == AMPACHE
        transform = self.transform
        append = self.list_store.append

        for i in xrange(100):
            if acc.keepalive == False:
                return False

            try:
                row = next_row()
            except sql.Error:
                return False

            if row:
                found += 1
                if ampache:
                    # Split the file into path and filename
                    try:
                        path, fname = row[6].rsplit("/", 1)
                    except IndexError:
                        continue
                
                    append((found, ) + row[:6] + (fname, transform(path))
                                                                    + row[8:])
                else:
                    append((found, ) + row[:7] + (transform(row[7]), )
                                                                    + row[8:])
            else:
                if found:
                    self.tree_cols[0].set_title("(%s)" % found)
                    self.tree_view.set_model(self.list_store)
                return False

        namespace[1] = (found, )
        return True


class MediaPane(gtk.Frame):
    """Database song details are displayed in this widget."""

    def __init__(self):
        gtk.Frame.__init__(self)
        self.set_shadow_type(gtk.SHADOW_IN)
        self.set_border_width(6)
        self.set_label_align(0.5, 0.5)
        main_vbox = gtk.VBox()
        self.add(main_vbox)
        self.notebook = gtk.Notebook()
        main_vbox.pack_start(self.notebook)
        
        self._tree_page = TreePage(self.notebook)
        self._flat_page = FlatPage(self.notebook)
        
        self.prefs_controls = PrefsControls()
        self.prefs_controls.bind(self._dbtoggle)

        main_vbox.show_all()

    def repair_focusability(self):
        self._tree_page.repair_focusability()
        self._flat_page.repair_focusability()

    def get_col_widths(self, keyval):
        """Grab column widths as textual data."""
        
        try:
            target = getattr(self, keyval)
        except AttributeError:
            return ""
        else:
            return target.get_col_widths()
    
    def set_col_widths(self, keyval, data):
        """Column widths are to be restored on application restart."""
        
        if data:
            try:
                target = getattr(self, keyval)
            except AttributeError:
                return
            else:
                target.set_col_widths(data)

    def _dbtoggle(self, conn_data, transform):
        if conn_data:
            # Connect and discover the database type.
            self._acc1 = DBAccessor(**conn_data)
            self._acc2 = DBAccessor(**conn_data)
            self._transform = transform
            self._acc1.request(('SHOW tables',), self._stage_1, self._fail_1)
        else:
            try:
                self._acc1.close()
                self._acc2.close()
            except AttributeError:
                pass
            else:
                self._tree_page.deactivate()
                self._flat_page.deactivate()
            self.hide()

    @staticmethod
    def schema_test(string, data):
        data = frozenset(x[0] for x in data)
        return frozenset(string.split()).issubset(data)
    
    ###########################################################################

    def _safe_disconnect(self):
        glib.idle_add(threadslock(self.prefs_controls.disconnect))
           
    def _hand_over(self, database_name):
        self._tree_page.activate(self._acc1, database_name, self._transform)
        self._flat_page.activate(self._acc2, database_name, self._transform)
        glib.idle_add(threadslock(self.show))
            
    def _fail_1(self, exception, notify):
        # Give up.
        self._safe_disconnect()

    def factory(dbtype):
        def inner(self, exception, notify):
            try:
                code = exception.args[0]
            except IndexError:
                pass
            else:
                if code != 1061:
                    notify_('Failed to create FULLTEXT index')
            self._hand_over(dbtype)
        return inner
    _fail_2, _fail_3 = [factory(x) for x in (PROKYON_3, AMPACHE)]
    del factory, x

    def _stage_1(self, acc, request, cursor, notify, rows):
        """Running under the accessor worker thread!
        
        Step 1 Identifying database type.
        """
        
        data = cursor.fetchall()
        if self.schema_test("tracks", data):
            request(('DESCRIBE tracks',), self._stage_2, self._fail_1)
        elif self.schema_test("album artist song", data):
            request(('DESCRIBE song',), self._stage_4, self._fail_1)
        else:
            notify(_('Unrecognised database'))
            self._safe_disconnect()
            
    def _stage_2(self, acc, request, cursor, notify, rows):
        """Confirm it's a Prokyon 3 database."""
        
        if self.schema_test("artist title album tracknumber bitrate " 
                                        "path filename", cursor.fetchall()):
            notify(_('Found Prokyon 3 schema'))
            # Try to add a FULLTEXT database.
            request(("""ALTER TABLE tracks ADD FULLTEXT artist (artist,title,
                        album,filename)""",), self._stage_3, self._fail_2)
        else:
            notify(_('Unrecognised database'))
            self._safe_disconnect()

    def _stage_3(self, acc, request, cursor, notify, rows):
        notify('Fulltext index added')
        self._hand_over('Prokyon 3')

    def _stage_4(self, acc, request, cursor, notify, rows):
        """Test for Ampache database."""

        if self.schema_test("artist title album track bitrate file", 
                                                            cursor.fetchall()):
            request(('DESCRIBE artist',), self._stage_5, self._fail_1)
        else:
            notify('Unrecognised database')
            self._safe_disconnect()

    def _stage_5(self, acc, request, cursor, notify, rows):
        if self.schema_test("name prefix", cursor.fetchall()):
            request(('DESCRIBE artist',), self._stage_6, self._fail_1)
        else:
            notify('Unrecognised database')
            self._safe_disconnect()

    def _stage_6(self, acc, request, cursor, notify, rows):
        if self.schema_test("name prefix", cursor.fetchall()):
            notify('Found Ampache schema')
            request(("ALTER TABLE album ADD FULLTEXT idjc (name)",),
                                                    self._stage_7, self._fail_3)
        else:
            notify('Unrecognised database')
            self._safe_disconnect()
        
    def _stage_7(self, acc, request, cursor, notify, rows):
        request(("ALTER TABLE artist ADD FULLTEXT idjc (name)",), self._stage_8,
                                                                self._fail_3)
        
    def _stage_8(self, acc, request, cursor, notify, rows):
        request(("ALTER TABLE song ADD FULLTEXT idjc (title)",), self._stage_9,
                                                                self._fail_3)

    def _stage_9(self, acc, request, cursor, notify, rows):
        notify('Fulltext index added')
        self._hand_over(AMPACHE)