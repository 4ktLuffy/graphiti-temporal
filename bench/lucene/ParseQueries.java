import org.apache.lucene.analysis.standard.StandardAnalyzer;
import org.apache.lucene.analysis.CharArraySet;
import org.apache.lucene.queryparser.classic.QueryParser;
public class ParseQueries {
  public static void main(String[] a) throws Exception {
    QueryParser qp = new QueryParser("name", new StandardAnalyzer(CharArraySet.EMPTY_SET));
    for (int i = 0; i < a.length; i++) {
      String s = a[i];
      try { System.out.println(s + "  =>  " + qp.parse(s)); }
      catch (Exception e) { System.out.println(s + "  =>  ERROR " + e.getClass().getSimpleName()); }
    }
  }
}
