package com.javaproject.demo.generator;

import org.mybatis.generator.api.MyBatisGenerator;
import org.mybatis.generator.config.Configuration;
import org.mybatis.generator.config.xml.ConfigurationParser;
import org.mybatis.generator.exception.InvalidConfigurationException;
import org.mybatis.generator.exception.XMLParserException;
import org.mybatis.generator.internal.DefaultShellCallback;

import java.io.IOException;
import java.io.InputStream;
import java.util.ArrayList;
import java.util.List;

public class Generator {
     public static void main(String[] args) throws IOException, Exception, InvalidConfigurationException {
         List<String> warnings = new ArrayList<>();
         boolean overwrite = true;

         InputStream is = Generator.class.getResourceAsStream("resources/generator/generatorConfig.xml");
         ConfigurationParser cp = new ConfigurationParser(warnings);
         Configuration configuration = cp.parseConfiguration(is);
         is.close();

         DefaultShellCallback callback = new DefaultShellCallback(overwrite);

         MyBatisGenerator myBatisGenerator = new MyBatisGenerator(configuration, callback, warnings);
         myBatisGenerator.generate(null);
         for (String warning : warnings) {
             System.out.println(warning);
         }

     }
}
